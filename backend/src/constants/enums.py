from enum import StrEnum


class Role(StrEnum):
    VIEWER = 'viewer'
    ANALYST = 'analyst'
    SUPERVISOR = 'supervisor'


class RunStatus(StrEnum):
    QUEUED = 'queued'
    SUCCEEDED = 'succeeded'
    FAILED = 'failed'


class Decision(StrEnum):
    APPROVED = 'approved'
    RETURNED = 'returned'


class CorrectionStatus(StrEnum):
    PENDING = 'pending'      # 已收下，等待缺号补齐
    APPLIED = 'applied'      # 已入账，件数已挪动
    REJECTED = 'rejected'    # 原格余额不足，整笔拒绝
    CONFLICT = 'conflict'    # 同号内容与已收更正不一致
