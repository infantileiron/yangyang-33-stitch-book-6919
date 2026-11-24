import pytest
from sqlalchemy import inspect, select, text

from src.constants.enums import RunStatus
from src.domain.analysis import analyze, seed
from src.models.base import Base, Session, engine
from src.models.workflow import AnalysisRun, Observation
from src.services.observations import add_observations, clean_observation
from src.services.runs import create_run, run_pending


class _RunRequest:
    def __init__(self, title='排期复核', parameters=None):
        self.title = title
        self.parameters = parameters or {}


def test_seed_analysis_still_works_with_observation_rows():
    rows = [clean_observation(row) for row in seed()]
    result = analyze(rows, {})
    assert {item['key'] for item in result['columns']}
    assert result['metrics'][0]['value'] == sum(row['count'] for row in rows)
    # 观测行仍按交付单编号做重复校验
    with pytest.raises(ValueError, match='重复汇总键'):
        analyze(rows + rows[:1], {})


def test_full_run_flow_snapshot_and_export(client, auth, seed_ledger):
    created = client.post('/api/runs', json={'title': '回归分析', 'parameters': {}},
                          headers=auth['analyst'])
    assert created.status_code == 201, created.text
    run_id = created.json()['id']

    # worker 入口直接驱动一次计算
    with Session.begin() as db:
        assert run_pending(db) is True
    detail = client.get(f'/api/runs/{run_id}', headers=auth['analyst']).json()
    assert detail['status'] == RunStatus.SUCCEEDED
    assert detail['result']['metrics']
    export = client.get(f'/api/runs/{run_id}/export', headers=auth['analyst'])
    assert export.status_code == 200
    assert 'text/csv' in export.headers['content-type']

    # viewer 在签核前看不到，签核后可以
    assert client.get(f'/api/runs/{run_id}', headers=auth['viewer']).status_code == 404
    review = client.post(f'/api/runs/{run_id}/review',
                         json={'decision': 'approved', 'note': '口径与明细核对通过'},
                         headers=auth['supervisor'])
    assert review.status_code == 201, review.text
    assert client.get(f'/api/runs/{run_id}', headers=auth['viewer']).status_code == 200


def test_empty_ledger_blocked_after_all_moved_out(database, seed_ledger):
    from src.services.corrections import submit_correction
    # SC-02 只有一个格 4 件，全部挪到 SC-01 之外的格仍保留件数；这里验证无观测时的拦截
    with Session.begin() as db:
        db.query(Observation).delete()
        with pytest.raises(Exception, match='请先导入观测记录'):
            create_run(db, _RunRequest(), 'analyst@demo.local')


def test_ensure_schema_adds_column_to_legacy_database(database):
    # 模拟旧版库：删掉新增列后重启 lifespan 逻辑应自动补列
    with engine.begin() as connection:
        connection.execute(text('ALTER TABLE analysis_runs DROP COLUMN ledger_snapshot'))
    inspector = inspect(engine)
    assert 'ledger_snapshot' not in {col['name'] for col in inspector.get_columns('analysis_runs')}

    from src.main import ensure_schema
    ensure_schema()
    inspector = inspect(engine)
    assert 'ledger_snapshot' in {col['name'] for col in inspector.get_columns('analysis_runs')}

    # 补列后新分析照常写入快照，历史行（快照为空）走兼容分支
    rows = [clean_observation(row) for row in seed()[:8]]
    with Session.begin() as db:
        add_observations(db, rows, 'analyst@demo.local', 'observation.created')
        run_id = create_run(db, _RunRequest('迁移后分析'), 'analyst@demo.local').id
    with Session.begin() as db:
        assert run_pending(db) is True
    with Session() as db:
        run = db.get(AnalysisRun, run_id)
        assert run.status == RunStatus.SUCCEEDED
        assert run.ledger_snapshot and run.ledger_snapshot['rows']
