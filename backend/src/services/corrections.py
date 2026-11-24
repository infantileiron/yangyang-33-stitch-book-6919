import hashlib
import json
from collections import defaultdict

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from src.config.project import SPEC
from src.constants.enums import CorrectionStatus
from src.models.base import utc_now
from src.models.workflow import AuditEvent, Correction, Observation
from src.services.errors import BusinessError
from src.services.validation import validated

# 参与同号比对的更正内容（序号与交付单编号确定更正槽位，不参与内容比对）
CONTENT_FIELDS = ('from_severity', 'from_delay', 'to_severity', 'to_delay', 'quantity')


def clean_correction(raw):
    row = validated(SPEC['correction_fields'], raw)
    if (row['from_severity'], row['from_delay']) == (row['to_severity'], row['to_delay']):
        raise BusinessError('原格与目标格相同，无需更正')
    return row


def content_key(row):
    body = {field: row[field] for field in CONTENT_FIELDS}
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


def sheet_process(db, sheet):
    """交付单在台账中的工序由观测记录推导，一张交付单只能对应一个工序。"""
    processes = {payload['process'] for payload in db.scalars(select(Observation.payload))
                 if payload.get('sheet') == sheet}
    if not processes:
        raise BusinessError(f'台账中没有交付单「{sheet}」的记录，无法受理更正')
    if len(processes) > 1:
        raise BusinessError(f'交付单「{sheet}」在台账中对应多个工序，请先核对台账')
    return next(iter(processes))


def ledger_cells(db):
    """当前已入账台账：观测叠加入账更正后的各损伤延期格件数。"""
    cells = defaultdict(int)
    for payload in db.scalars(select(Observation.payload)):
        cells[(payload['process'], payload['severity'], payload['delay'])] += payload['count']
    applied = select(Correction).where(Correction.status == CorrectionStatus.APPLIED)
    for item in db.scalars(applied):
        cells[(item.process, item.from_severity, item.from_delay)] -= item.quantity
        cells[(item.process, item.to_severity, item.to_delay)] += item.quantity
    return cells


def ledger_rows(db):
    return [dict(process=process, severity=severity, delay=delay, count=count)
            for (process, severity, delay), count in sorted(ledger_cells(db).items()) if count > 0]


def _drain_pending(db, sheet, cells):
    """按序号从小到大入账：缺号即停，原格不够扣则该笔整笔拒绝并继续下一号。"""
    terminal_seqs = set(db.scalars(select(Correction.seq).where(
        Correction.sheet == sheet, Correction.status != CorrectionStatus.PENDING)))
    expected = max(terminal_seqs, default=0) + 1
    transitions = []
    while True:
        item = db.scalar(select(Correction).where(
            Correction.sheet == sheet, Correction.seq == expected).with_for_update())
        if item is None:
            break  # 缺号，等补齐后再入账
        source = (item.process, item.from_severity, item.from_delay)
        if cells[source] < item.quantity:
            item.status = CorrectionStatus.REJECTED
            item.reason = (f'原格（损伤 {item.from_severity} 级/延期 {item.from_delay} 级）'
                           f'余额 {cells[source]} 件，不足挪动 {item.quantity} 件，整笔拒绝')
        else:
            cells[source] -= item.quantity
            cells[(item.process, item.to_severity, item.to_delay)] += item.quantity
            item.status = CorrectionStatus.APPLIED
            item.posted_at = utc_now()
        transitions.append(item)
        expected += 1
    return transitions


def submit_correction(db, raw, actor):
    row = clean_correction(raw)
    process = sheet_process(db, row['sheet'])
    key = content_key(row)

    existing = db.scalar(select(Correction).where(
        Correction.sheet == row['sheet'], Correction.seq == row['seq']).with_for_update())
    if existing is None:
        # 并发下唯一约束可能抢先：回滚到保存点后按重试/冲突处理，不把整笔事务带崩
        try:
            with db.begin_nested():
                item = Correction(sheet=row['sheet'], seq=row['seq'], process=process,
                                  from_severity=row['from_severity'], from_delay=row['from_delay'],
                                  to_severity=row['to_severity'], to_delay=row['to_delay'],
                                  quantity=row['quantity'], content_key=key,
                                  status=CorrectionStatus.PENDING)
                db.add(item)
                db.flush()
        except IntegrityError:
            existing = db.scalar(select(Correction).where(
                Correction.sheet == row['sheet'], Correction.seq == row['seq']).with_for_update())
    if existing is not None:
        if existing.content_key != key:
            raise BusinessError(
                f'交付单「{row["sheet"]}」{row["seq"]} 号更正内容与已收记录不一致，冲突拒绝', 409)
        if existing.status != CorrectionStatus.PENDING:
            return existing, False  # 同一更正重试：原样返回，不重复挪数
        item = existing  # 仍是挂起态：缺号可能已由别的事务补齐，借这次重试再排一次队
    else:
        db.add(AuditEvent(actor=actor, action='correction.received',
                          detail=f'交付单 {item.sheet} · {item.seq} 号更正已收下'))

    cells = ledger_cells(db)
    transitions = _drain_pending(db, item.sheet, cells)
    for posted in transitions:
        if posted.status == CorrectionStatus.APPLIED:
            action, word = 'correction.applied', '已入账'
        else:
            action, word = 'correction.rejected', '余额不足整笔拒绝'
        db.add(AuditEvent(actor=actor, action=action,
                          detail=f'交付单 {posted.sheet} · {posted.seq} 号更正{word}'))
    return item, existing is None


def list_corrections(db, sheet=None, offset=0, limit=20):
    query = select(Correction)
    count_query = select(func.count(Correction.id))
    if sheet:
        query = query.where(Correction.sheet == sheet)
        count_query = count_query.where(Correction.sheet == sheet)
    total = db.scalar(count_query)
    items = db.scalars(query.order_by(Correction.id.desc()).offset(offset).limit(limit)).all()
    return total, items
