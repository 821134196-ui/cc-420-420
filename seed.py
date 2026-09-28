"""演示数据预置：楼栋、租户、人员、权限组、发卡、挂失补卡、离场回收、
临时卡以及离线设备补传场景。姓名 / 卡号 / 门牌均为演示数据。

用法：
    python3 seed.py [数据库路径]
"""

from __future__ import annotations

from datetime import datetime, timedelta

from core import (
    AccessSystem, CARD_AVAILABLE, CARD_DAMAGED, CARD_LOST, CARD_REVOKED,
    CARD_TEMP, EV, ROLE_ADMIN, ROLE_CLERK,
)

STAFF = [
    ("admin", "林岚", ROLE_ADMIN),
    ("clerk_a", "沈括", ROLE_CLERK),
    ("clerk_b", "周知音", ROLE_CLERK),
    ("auditor", "顾审计", "auditor"),
]


def seed(system: AccessSystem, *, offline_demo: bool = True) -> None:
    c = system.conn.cursor()

    # 岗位账号
    c.executemany(
        "INSERT INTO admin_users(username,display_name,role) VALUES (?,?,?)", STAFF)

    # 楼栋
    c.execute("INSERT INTO buildings(name,floors) VALUES ('云阶大厦', 12)")
    building_id = c.lastrowid

    # 租户与门牌
    tenants = [
        (building_id, "星河设计事务所", "A-1201", 12),
        (building_id, "青岑科技有限公司", "A-0806", 8),
        (building_id, "白鹭翻译社", "A-0503", 5),
        (building_id, "岚桥咨询", "A-0302", 3),
        (building_id, "潮汐文化传媒", "A-1008", 10),
    ]
    c.executemany(
        "INSERT INTO tenants(building_id,name,unit_no,floor) VALUES (?,?,?,?)", tenants)
    tenant_ids = [r[0] for r in c.execute(
        "SELECT id FROM tenants ORDER BY id").fetchall()]

    # 人员（演示姓名 / 证件号 / 电话）
    people = [
        ("陆昭",   "110101199003071234", "13800001111", tenant_ids[0]),  # 0
        ("沈知微", "310104199207152233", "13800002222", tenant_ids[0]),  # 1
        ("程述",   "440305198811203012", "13800003333", tenant_ids[1]),  # 2
        ("阮清越", "330106199506014421", "13800004444", tenant_ids[1]),  # 3
        ("白栖迟", "510107199101256677", "13800005555", tenant_ids[2]),  # 4
        ("柳眠",   "320105199409087788", "13800006666", tenant_ids[2]),  # 5
        ("霍连峰", "120103198704118899", "13800007777", tenant_ids[3]),  # 6
        ("蔺雨时", "350203199607239900", "13800008888", tenant_ids[3]),  # 7
        ("薛葡萄", "610102199312030011", "13800009999", tenant_ids[4]),  # 8
        ("简言",   "110108199702120022", "13800001010", tenant_ids[4]),  # 9
    ]
    c.executemany(
        "INSERT INTO people(name,id_no,phone,tenant_id,status) VALUES (?,?,?,?,'active')",
        people)
    pids = [r[0] for r in c.execute("SELECT id FROM people ORDER BY id").fetchall()]
    system.conn.commit()

    today = datetime.now().date()
    iso = lambda d: d.isoformat()

    # 权限组（楼层 + 时段 + 生效日期）
    g1 = system.create_group("楼内办公组", "工作日全时段，本层与大堂", role=ROLE_ADMIN)
    g2 = system.create_group("设备维护组", "机房楼层，窄时段", role=ROLE_ADMIN)
    g3 = system.create_group("租户管理者组", "本租户所在楼层 + 会议室层", role=ROLE_ADMIN)
    g4 = system.create_group("访客临时组", "大堂与会议层，限定日期", role=ROLE_ADMIN)

    def rule(gid, floor, s="00:00", e="23:59", days_from=0, days_to=365):
        system.set_group_rule(gid, floor, s, e,
                              iso(today + timedelta(days=days_from)),
                              iso(today + timedelta(days=days_to)),
                              confirm=True, role=ROLE_ADMIN, actor="林岚")

    # 办公组：大堂(1) + 8、12 层（两个租户的人共用），工作日白班
    for f in (1, 8, 12):
        rule(g1, f, "08:00", "20:00")
    # 维护组：1 层全天 + 机房 6 层窄时段
    rule(g2, 1, "00:00", "23:59")
    rule(g2, 6, "09:00", "11:30")
    # 管理者组：1、3、10、12
    for f in (1, 3, 10, 12):
        rule(g3, f, "07:30", "22:00")
    # 访客组：1、12，且仅未来 7 天有效
    rule(g4, 1, "09:00", "18:00", days_to=7)
    rule(g4, 12, "09:00", "18:00", days_to=7)

    # ---- 正常发卡（A/B 两柜台） ----------------------------------------
    system.issue_card("AC-1001", pids[0], g1, counter="A柜台", role=ROLE_CLERK, actor="沈括")
    system.issue_card("AC-1002", pids[1], g1, counter="A柜台", role=ROLE_CLERK, actor="沈括")
    system.issue_card("AC-1003", pids[2], g1, counter="B柜台", role=ROLE_CLERK, actor="周知音")
    system.issue_card("AC-1004", pids[3], g1, counter="B柜台", role=ROLE_CLERK, actor="周知音")
    system.issue_card("AC-2001", pids[6], g3, counter="A柜台", role=ROLE_CLERK, actor="沈括")
    system.issue_card("AC-2002", pids[7], g3, counter="B柜台", role=ROLE_CLERK, actor="周知音")
    system.issue_card("AC-3001", pids[8], g3, counter="A柜台", role=ROLE_CLERK, actor="沈括")

    # 维护组卡
    system.issue_card("MC-0001", pids[4], g2, counter="B柜台", role=ROLE_CLERK, actor="周知音")

    # ---- 挂失 + 补卡（新卡保留旧卡关系） -------------------------------
    system.report_lost("AC-1002", role=ROLE_CLERK, actor="沈括")
    system.reissue_card("AC-1002", "AC-1002R", role=ROLE_CLERK, actor="沈括")

    # ---- 损坏 ----------------------------------------------------------
    system.issue_card("AC-1005", pids[5], g1, counter="A柜台", role=ROLE_CLERK, actor="沈括")
    system.report_damaged("AC-1005", role=ROLE_CLERK, actor="沈括")

    # ---- 借用转交 ------------------------------------------------------
    # AC-1004（阮清越）转交给同公司的程述（pids[2] 已有卡，仅演示借用事件）
    system.transfer_card("AC-1004", pids[2], role=ROLE_CLERK, actor="周知音")

    # ---- 权限越界事件（留痕） ------------------------------------------
    # 办公组卡 AC-1001 刷 6 层机房 -> 拒绝
    system.evaluate_access("AC-1001", 6, record=True, actor="DEV-06")
    # 维护组卡 MC-0001 夜间刷 6 层 -> 时段越界
    night = datetime.now().replace(hour=22, minute=10, second=0, microsecond=0)
    system.evaluate_access("MC-0001", 6, at=night, record=True, actor="DEV-06")

    # ---- 临时卡：一张可用，一张已到期（自动失效） ----------------------
    soon = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d 18:00:00")
    system.issue_card("TC-9001", pids[9], g4, card_type=CARD_TEMP,
                      expires_at=soon, valid_to=iso(today + timedelta(days=2)),
                      counter="A柜台", role=ROLE_CLERK, actor="沈括")

    past = (datetime.now() - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
    system.issue_card("TC-9002", pids[4], g4, card_type=CARD_TEMP,
                      expires_at=past, valid_to=iso(today - timedelta(days=1)),
                      counter="B柜台", role=ROLE_CLERK, actor="周知音")
    system.expire_temp_cards()  # 预置一张到期失效的临时卡

    # ---- 离场回收 ------------------------------------------------------
    # 白鹭翻译社的白栖迟（MC-0001 持有）离场：冻结 + 回收记录
    system.recycle_card("MC-0001", reason="租户退租离场", role=ROLE_CLERK, actor="沈括")

    # ---- 同卡号两柜台同时发放（只允许一笔成功） ------------------------
    # 直接演示一次被拒事件（并发场景由测试用双线程验证）
    try:
        system.issue_card("AC-1001", pids[3], g1, counter="B柜台",
                          role=ROLE_CLERK, actor="周知音")
    except Exception as e:  # noqa: BLE001 - 预期失败，事件已落库
        assert "已被发放" in str(e)

    # ---- 设备离线 + 补传 -----------------------------------------------
    if offline_demo:
        system.set_device_online(False, role=ROLE_ADMIN, device_id="DEV-01")
        system.issue_card("AC-3002", pids[9], g3, counter="B柜台",
                          role=ROLE_CLERK, actor="周知音")
        # 保持离线，待启动后由管理员在界面上点“设备恢复并补传”
        # （不自动恢复，方便演示离线/补传事件与排队计数）

    return system


def build_system(db_path: str = ":memory:") -> AccessSystem:
    sys_ = AccessSystem(db_path)
    seed(sys_)
    return sys_


if __name__ == "__main__":
    import sys

    path = sys.argv[1] if len(sys.argv) > 1 else "access.db"
    s = build_system(path)
    n_cards = s.conn.execute("SELECT COUNT(*) c FROM cards").fetchone()["c"]
    n_people = s.conn.execute("SELECT COUNT(*) c FROM people").fetchone()["c"]
    n_events = s.conn.execute("SELECT COUNT(*) c FROM events").fetchone()["c"]
    print(f"演示数据已写入 {path}：楼栋1栋 / 租户5 / 人员{n_people} / "
          f"卡片{n_cards} / 事件{n_events}")
    s.close()
