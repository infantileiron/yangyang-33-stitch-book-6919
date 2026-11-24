import pytest
from sqlalchemy import func, select

from src.constants.enums import CorrectionStatus
from src.models.base import Session
from src.models.workflow import AnalysisRun, Correction
from src.services.corrections import ledger_rows, submit_correction
from src.services.errors import BusinessError
from src.services.runs import run_pending


def correction(seq, fs, fd, ts, td, quantity, sheet='SC-01'):
    return dict(sheet=sheet, seq=seq, from_severity=fs, from_delay=fd,
                to_severity=ts, to_delay=td, quantity=quantity)


def test_correction_out_of_order_held_until_gap_filled(database, seed_ledger):
    # 二号先到：收下但不入账
    with Session.begin() as db:
        item, created = submit_correction(db, correction(2, 4, 3, 3, 2, 2), 'analyst@demo.local')
        assert created is True
        assert item.status == CorrectionStatus.PENDING
    with Session() as db:
        assert ledger_rows(db) == _as_cells(seed_ledger)

    # 一号补齐：两号连号一起入账
    with Session.begin() as db:
        item, _ = submit_correction(db, correction(1, 4, 3, 3, 2, 3), 'analyst@demo.local')
        assert item.status == CorrectionStatus.APPLIED
    with Session() as db:
        statuses = {row.seq: row.status for row in db.scalars(select(Correction))}
        assert statuses == {1: CorrectionStatus.APPLIED, 2: CorrectionStatus.APPLIED}
        cells = {(r['severity'], r['delay']): r['count']
                 for r in ledger_rows(db) if r['process'] == '植鞣皮补色'}
        # 原格 8 - 3 - 2 = 3；目标格 5 + 3 + 2 = 10
        assert cells[(4, 3)] == 3
        assert cells[(3, 2)] == 10


def test_retry_same_correction_does_not_move_twice(database, seed_ledger):
    payload = correction(1, 4, 3, 3, 2, 3)
    with Session.begin() as db:
        first, created = submit_correction(db, payload, 'analyst@demo.local')
        assert created is True
    with Session.begin() as db:
        retry, created = submit_correction(db, payload, 'analyst@demo.local')
        assert created is False
        assert retry.id == first.id
        assert retry.status == CorrectionStatus.APPLIED
    with Session() as db:
        cells = {(r['severity'], r['delay']): r['count']
                 for r in ledger_rows(db) if r['process'] == '植鞣皮补色'}
        assert cells[(4, 3)] == 5  # 8 - 3，只扣一次
        assert cells[(3, 2)] == 8  # 5 + 3，只加一次


def test_retry_of_pending_drains_after_gap_filled(database, seed_ledger):
    payload_two = correction(2, 4, 3, 3, 2, 2)
    with Session.begin() as db:
        held, created = submit_correction(db, payload_two, 'analyst@demo.local')
        assert held.status == CorrectionStatus.PENDING
    # 补齐一号，二号入账
    with Session.begin() as db:
        submit_correction(db, correction(1, 4, 3, 3, 2, 3), 'analyst@demo.local')
    # 再重试二号：已入账，原样返回且不重复挪数
    with Session.begin() as db:
        retry, created = submit_correction(db, payload_two, 'analyst@demo.local')
        assert created is False
        assert retry.status == CorrectionStatus.APPLIED
    with Session() as db:
        cells = {(r['severity'], r['delay']): r['count']
                 for r in ledger_rows(db) if r['process'] == '植鞣皮补色'}
        assert cells[(4, 3)] == 3
        assert cells[(3, 2)] == 10


def test_pending_retry_before_gap_stays_pending_and_idempotent(database, seed_ledger):
    payload_two = correction(2, 4, 3, 3, 2, 2)
    with Session.begin() as db:
        first, created = submit_correction(db, payload_two, 'analyst@demo.local')
        assert created is True and first.status == CorrectionStatus.PENDING
    # 一号仍缺：重试二号不报错、不挪数、不新建
    with Session.begin() as db:
        retry, created = submit_correction(db, payload_two, 'analyst@demo.local')
        assert created is False and retry.id == first.id
        assert retry.status == CorrectionStatus.PENDING
    with Session() as db:
        assert ledger_rows(db) == _as_cells(seed_ledger)
        assert db.scalar(select(func.count(Correction.id))) == 1


def test_same_seq_different_content_is_conflict(database, seed_ledger):
    with Session.begin() as db:
        submit_correction(db, correction(1, 4, 3, 3, 2, 3), 'analyst@demo.local')
    with pytest.raises(BusinessError) as excinfo:
        with Session.begin() as db:
            submit_correction(db, correction(1, 4, 3, 3, 2, 4), 'analyst@demo.local')
    assert excinfo.value.status == 409
    assert '冲突' in str(excinfo.value)
    # 冲突不改变台账
    with Session() as db:
        cells = {(r['severity'], r['delay']): r['count']
                 for r in ledger_rows(db) if r['process'] == '植鞣皮补色'}
        assert cells[(4, 3)] == 5
        assert cells[(3, 2)] == 8


def test_insufficient_source_balance_rejects_whole_correction_and_keeps_seq(database, seed_ledger):
    with Session.begin() as db:
        item, _ = submit_correction(db, correction(1, 4, 3, 2, 2, 99), 'analyst@demo.local')
        assert item.status == CorrectionStatus.REJECTED
        assert item.reason and '不足' in item.reason
    # 原格件数原封不动
    with Session() as db:
        cells = {(r['severity'], r['delay']): r['count']
                 for r in ledger_rows(db) if r['process'] == '植鞣皮补色'}
        assert cells[(4, 3)] == 8
        assert (2, 2) not in cells
    # 拒绝笔占用一号：再来一号同号不同内容仍是冲突，二号不受影响可正常入账
    with pytest.raises(BusinessError):
        with Session.begin() as db:
            submit_correction(db, correction(1, 4, 3, 2, 2, 1), 'analyst@demo.local')
    with Session.begin() as db:
        item, _ = submit_correction(db, correction(2, 4, 3, 3, 2, 1), 'analyst@demo.local')
        assert item.status == CorrectionStatus.APPLIED


def test_partial_balance_is_also_rejected_not_partial_move(database, seed_ledger):
    # 原格 8 件，要挪 9 件：不允许只挪 8 件，整笔拒绝
    with Session.begin() as db:
        item, _ = submit_correction(db, correction(1, 4, 3, 3, 2, 9), 'analyst@demo.local')
        assert item.status == CorrectionStatus.REJECTED
    with Session() as db:
        cells = {(r['severity'], r['delay']): r['count']
                 for r in ledger_rows(db) if r['process'] == '植鞣皮补色'}
        assert cells[(4, 3)] == 8
        assert cells[(3, 2)] == 5


def test_rejected_then_gap_drain_continues_to_next_seq(database, seed_ledger):
    # 三号先到挂起；一号拒绝、二号入账后，三号应继续被处理
    with Session.begin() as db:
        item, _ = submit_correction(db, correction(3, 4, 3, 3, 2, 1), 'analyst@demo.local')
        assert item.status == CorrectionStatus.PENDING
    with Session.begin() as db:
        rejected, _ = submit_correction(db, correction(1, 4, 3, 2, 2, 99), 'analyst@demo.local')
        assert rejected.status == CorrectionStatus.REJECTED
        assert ledger_rows  # drain stops at missing seq 2
    with Session() as db:
        pending = db.scalar(select(Correction).where(Correction.seq == 3))
        assert pending.status == CorrectionStatus.PENDING
    with Session.begin() as db:
        second, _ = submit_correction(db, correction(2, 4, 3, 3, 2, 2), 'analyst@demo.local')
        assert second.status == CorrectionStatus.APPLIED
    with Session() as db:
        third = db.scalar(select(Correction).where(Correction.seq == 3))
        assert third.status == CorrectionStatus.APPLIED


def _as_cells(rows):
    aggregated = {}
    for row in rows:
        key = (row['process'], row['severity'], row['delay'])
        aggregated[key] = aggregated.get(key, 0) + row['count']
    return [dict(process=process, severity=severity, delay=delay, count=count)
            for (process, severity, delay), count in sorted(aggregated.items())]


class _RunRequest:
    def __init__(self, title='排期复核', parameters=None):
        self.title = title
        self.parameters = parameters or {}


def _create_and_run(db, title='排期复核'):
    from src.services.runs import create_run
    run = create_run(db, _RunRequest(title), 'analyst@demo.local')
    run_id = run.id
    db.flush()
    return run_id


def test_run_uses_posted_ledger_snapshot_and_keeps_old_counts(database, seed_ledger):
    # 先做一笔已入账更正：四号格 8 -> 5，三号格 5 -> 8
    with Session.begin() as db:
        submit_correction(db, correction(1, 4, 3, 3, 2, 3), 'analyst@demo.local')

    with Session.begin() as db:
        run_id = _create_and_run(db, '第一次分析')
        snapshot = db.get(AnalysisRun, run_id).ledger_snapshot['rows']
        cells = {(r['severity'], r['delay']): r['count']
                 for r in snapshot if r['process'] == '植鞣皮补色'}
        assert cells[(4, 3)] == 5
        assert cells[(3, 2)] == 8

    # 排队期间补齐的新更正：不应改变已冻结的第一次分析
    with Session.begin() as db:
        run_pending(db)  # 计算第一次分析
    with Session.begin() as db:
        submit_correction(db, correction(2, 4, 3, 3, 2, 2), 'analyst@demo.local')

    with Session() as db:
        first = db.get(AnalysisRun, run_id)
        first_cells = {(r['severity'], r['delay']): r['count']
                       for r in first.ledger_snapshot['rows'] if r['process'] == '植鞣皮补色'}
        assert first_cells[(4, 3)] == 5  # 旧报告计数保持原样
        assert first_cells[(3, 2)] == 8
        # 旧结果由冻结快照算出
        process_rows = [row for row in first.result['rows'] if row.get('process') == '植鞣皮补色'
                        and row.get('grade') is None][0]
        assert process_rows['count'] == 13  # 8+5，件数守恒

    # 下次分析才纳入排队期间补齐的更正
    with Session.begin() as db:
        second_id = _create_and_run(db, '第二次分析')
        run_pending(db)
    with Session() as db:
        second = db.get(AnalysisRun, second_id)
        second_cells = {(r['severity'], r['delay']): r['count']
                        for r in second.ledger_snapshot['rows'] if r['process'] == '植鞣皮补色'}
        assert second_cells[(4, 3)] == 3  # 5 - 2
        assert second_cells[(3, 2)] == 10  # 8 + 2


def test_pending_corrections_excluded_from_run(database, seed_ledger):
    # 二号先到挂起，发起分析时只能按原台账算
    with Session.begin() as db:
        item, _ = submit_correction(db, correction(2, 4, 3, 3, 2, 2), 'analyst@demo.local')
        assert item.status == CorrectionStatus.PENDING
    with Session.begin() as db:
        run_id = _create_and_run(db)
    with Session() as db:
        snapshot = db.get(AnalysisRun, run_id).ledger_snapshot['rows']
        cells = {(r['severity'], r['delay']): r['count']
                 for r in snapshot if r['process'] == '植鞣皮补色'}
        assert cells[(4, 3)] == 8
        assert cells[(3, 2)] == 5


def test_snapshot_total_pieces_conserved(database, seed_ledger):
    with Session.begin() as db:
        submit_correction(db, correction(1, 4, 3, 3, 2, 3), 'analyst@demo.local')
        submit_correction(db, correction(2, 3, 2, 2, 1, 4), 'analyst@demo.local')
        run_id = _create_and_run(db)
        run_pending(db)
    with Session() as db:
        run = db.get(AnalysisRun, run_id)
        total = sum(row['count'] for row in run.ledger_snapshot['rows'])
        assert total == 17  # 8+5+4，挪格不改总件数
        result_total = next(metric['value'] for metric in run.result['metrics']
                            if metric['label'] == '交付件数')
        assert result_total == 17


def test_api_submit_list_ledger_and_permissions(client, auth, seed_ledger):
    payload = correction(2, 4, 3, 3, 2, 2)
    # 只读角色不能受理更正
    denied = client.post('/api/corrections', json=payload, headers=auth['viewer'])
    assert denied.status_code == 403
    # 分析员受理：二号缺一号，状态挂起
    response = client.post('/api/corrections', json=payload, headers=auth['analyst'])
    assert response.status_code == 201
    body = response.json()
    assert body['status'] == CorrectionStatus.PENDING
    assert body['sheet'] == 'SC-01'
    # 同号同内容重试：200 且不重复
    retry = client.post('/api/corrections', json=payload, headers=auth['analyst'])
    assert retry.status_code == 200
    assert retry.json()['id'] == body['id']
    # 同号不同内容：409 冲突
    conflict = client.post('/api/corrections',
                           json=correction(2, 4, 3, 3, 2, 9), headers=auth['analyst'])
    assert conflict.status_code == 409
    # 补齐一号，两号入账
    first = client.post('/api/corrections',
                        json=correction(1, 4, 3, 3, 2, 3), headers=auth['supervisor'])
    assert first.status_code == 201
    assert first.json()['status'] == CorrectionStatus.APPLIED
    # 列表可按交付单过滤
    listing = client.get('/api/corrections?sheet=SC-01', headers=auth['viewer'])
    assert listing.status_code == 200
    assert listing.json()['total'] == 2
    assert {item['status'] for item in listing.json()['items']} == {CorrectionStatus.APPLIED}
    # 台账视图反映挪动结果
    ledger = client.get('/api/corrections/ledger', headers=auth['viewer']).json()['items']
    cells = {(r['severity'], r['delay']): r['count']
             for r in ledger if r['process'] == '植鞣皮补色'}
    assert cells[(4, 3)] == 3
    assert cells[(3, 2)] == 10
    # 余额不足的整笔拒绝通过 API 可见
    rejected = client.post('/api/corrections',
                           json=correction(3, 4, 3, 2, 2, 99), headers=auth['analyst'])
    assert rejected.json()['status'] == CorrectionStatus.REJECTED
    assert rejected.json()['reason']


def test_api_validation_errors(client, auth, seed_ledger):
    # 原格目标格相同
    same = client.post('/api/corrections',
                       json=correction(1, 4, 3, 4, 3, 1), headers=auth['analyst'])
    assert same.status_code == 422
    # 序号必须 >= 1
    bad_seq = client.post('/api/corrections',
                          json=correction(0, 4, 3, 3, 2, 1), headers=auth['analyst'])
    assert bad_seq.status_code == 422
    # 未知交付单
    unknown = client.post('/api/corrections',
                          json=correction(1, 4, 3, 3, 2, 1, sheet='SC-404'),
                          headers=auth['analyst'])
    assert unknown.status_code == 422
    # 未登录
    assert client.post('/api/corrections', json=correction(1, 4, 3, 3, 2, 1)).status_code == 401
