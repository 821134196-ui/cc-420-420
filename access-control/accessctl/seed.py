"""演示数据预置：楼栋、租户、人员、权限组、库存卡，并走完整业务流。"""
from __future__ import annotations

from datetime import datetime, timedelta

from .db import Database
from .services import Service, now_iso, mask_card_no


def is_seeded(db: Database) -> bool:
    return db.conn.execute("SELECT COUNT(*) FROM operators").fetchone()[0] > 0


def seed(db: Database) -> None:
    if is_seeded(db):
        return
    s = Service(db)

    # ------------------------------------------------------------ 操作员
    operators = [
        ("admin01", "林岚", "admin"),
        ("clerk01", "王晨", "clerk"),
        ("clerk02", "赵宁", "clerk"),
        ("guard01", "陈磊", "guard"),
        ("audit01", "周敏", "auditor"),
    ]
    with db.tx() as conn:
        for login, name, role in operators:
            conn.execute(
                "INSERT INTO operators(login,name,role,active) VALUES(?,?,?,1)",
                (login, name, role))
    ADMIN, CLERK1, CLERK2, GUARD, AUDITOR = 1, 2, 3, 4, 5

    # ------------------------------------------------------------ 楼栋 / 房间
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO buildings(name,address,floors) VALUES(?,?,?)",
            ("云升大厦 A 座", "云升路 88 号", "1,2,3,4,5,6"))
        conn.execute(
            "INSERT INTO buildings(name,address,floors) VALUES(?,?,?)",
            ("云升大厦 B 座", "云升路 88 号附楼", "1,2,3,4"))
        tenants = [
            ("星岚科技有限公司", "顾一帆", "138-0000-1001", "A座 401"),
            ("云栖设计工作室", "沈知微", "138-0000-2002", "A座 502"),
            ("恒信物业管理处", "高敬业", "138-0000-3003", "A座 101"),
        ]
        for t in tenants:
            conn.execute(
                "INSERT INTO tenants(name,contact_person,phone,unit) VALUES(?,?,?,?)",
                t)

        def room(bid, floor, no, label, tid=None):
            conn.execute(
                "INSERT INTO rooms(building_id,floor,room_no,label,tenant_id)"
                " VALUES(?,?,?,?,?)", (bid, floor, no, label, tid))

        # A 座
        room(1, 1, "A-101", "物业服务大厅", 3)
        room(1, 1, "A-102", "大堂前台", 3)
        room(1, 3, "A-301", "星岚科技·研发部", 1)
        room(1, 4, "A-401", "星岚科技·总经理室", 1)
        room(1, 4, "A-402", "星岚科技·会议室", 1)
        room(1, 5, "A-501", "云栖设计·开放工区", 2)
        room(1, 5, "A-502", "云栖设计·创意室", 2)
        room(1, 6, "A-601", "设备机房", 3)
        # B 座
        room(2, 1, "B-101", "配套商铺", None)
        room(2, 3, "B-301", "共享会议室", None)

    # ------------------------------------------------------------ 人员
    people = [
        # 姓名, 工号, 租户, 房间, 岗位, 电话
        ("顾一帆", "P1001", 1, 4, "总经理", "139-1111-0001"),
        ("林晓彤", "P1002", 1, 3, "研发工程师", "139-1111-0002"),
        ("程远", "P1003", 1, 3, "研发工程师", "139-1111-0003"),
        ("许蔓", "P1004", 1, 4, "行政主管", "139-1111-0004"),
        ("沈知微", "P1005", 2, 7, "设计总监", "139-2222-0005"),
        ("骆白", "P1006", 2, 6, "平面设计师", "139-2222-0006"),
        ("高敬业", "P1007", 3, 1, "物业经理", "139-3333-0007"),
        ("秦师傅", "P1008", 3, 8, "维保技工", "139-3333-0008"),
        ("唐小满", "P1009", 1, 3, "实习生", "139-1111-0009"),
        ("韩素", "P1010", 2, 6, " UI 设计师", "139-2222-0010"),
        ("罗振", "P1011", 1, 3, "产品经理", "139-1111-0011"),
        ("魏来", "P1012", None, None, "外包驻场(已离场)", "139-0000-0012"),
    ]
    with db.tx() as conn:
        for name, no, tid, rid, pos, phone in people:
            conn.execute(
                "INSERT INTO people(name,person_no,tenant_id,room_id,position,phone,"
                "status) VALUES(?,?,?,?,?,?,'active')",
                (name, no, tid, rid, pos, phone))

    # ------------------------------------------------------------ 权限组
    ts = now_iso()
    groups = [
        # name, desc, floors, start, end, valid_from, valid_to
        ("大堂通行组", "仅可进入 1 层大堂", [1], "00:00", "23:59", None, None),
        ("星岚员工组", "工作日时段通行 1/3/4 层", [1, 3, 4], "08:00", "20:00",
         "2026-01-01", "2026-12-31"),
        ("云栖设计组", "通行 1/5 层，09:00–19:00", [1, 5], "09:00", "19:00",
         "2026-01-01", "2026-12-31"),
        ("物业维保组", "全楼层 06:00–22:00", [1, 2, 3, 4, 5, 6], "06:00", "22:00",
         None, None),
        ("访客临时组", "临时访客：大堂与 3 层", [1, 3], "09:00", "18:00", None, None),
    ]
    with db.tx() as conn:
        for name, desc, floors, t0, t1, vf, vt in groups:
            cur = conn.execute(
                "INSERT INTO permission_groups(name,description,time_start,time_end,"
                "valid_from,valid_to,builtin,created_at) VALUES(?,?,?,?,?,?,1,?)",
                (name, desc, t0, t1, vf, vt, ts))
            gid = cur.lastrowid
            conn.executemany(
                "INSERT INTO group_floors(group_id,floor) VALUES(?,?)",
                [(gid, f) for f in floors])

    # ------------------------------------------------------------ 设备
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO devices(name,location,building_id,status,last_seen)"
            " VALUES(?,?,?, 'online',?)",
            ("A座东门闸机 D-01", "A座一层东门", 1, now_iso()))
        conn.execute(
            "INSERT INTO devices(name,location,building_id,status,last_seen)"
            " VALUES(?,?,?, 'online',?)",
            ("A座消防门 D-02", "A座三层西侧", 1, now_iso()))
        conn.execute(
            "INSERT INTO devices(name,location,building_id,status,last_seen)"
            " VALUES(?,?,?, 'offline',?)",
            ("B座南门闸机 D-03", "B座一层南门（网络故障）", 2, now_iso()))

    # ------------------------------------------------------------ 库存卡
    with db.tx() as conn:
        for i in range(1, 17):
            conn.execute(
                "INSERT INTO cards(card_no,card_type,status,batch_no) VALUES("
                "'A-10%02d','normal','in_stock','BATCH-2026-09')" % i)
        for i in range(1, 5):
            conn.execute(
                "INSERT INTO cards(card_no,card_type,status,batch_no) VALUES("
                "'T-20%02d','temporary','in_stock','BATCH-2026-T')" % i)

    # ------------------------------------------------------------ 业务流：发卡
    # 人员 → 权限组
    issues = [
        (1, 2),   # 顾一帆  星岚员工组
        (2, 2),   # 林晓彤
        (3, 2),   # 程远
        (4, 2),   # 许蔓
        (5, 3),   # 沈知微  云栖设计组
        (6, 3),   # 骆白
        (7, 4),   # 高敬业  物业维保组
        (8, 4),   # 秦师傅
        (9, 2),   # 唐小满  实习生
        (10, 3),  # 韩素
    ]
    stock = [r[0] for r in db.conn.execute(
        "SELECT card_no FROM cards WHERE status='in_stock' AND card_type='normal'"
        " ORDER BY id")]
    idx = 0
    for person_id, group_id in issues:
        d = s.create_issue_draft(CLERK1, stock[idx], person_id, group_id,
                                 valid_from="2026-09-01", valid_to="2026-12-31",
                                 note="柜台批量发卡")
        s.confirm_issue_draft(CLERK1, d["id"])
        idx += 1

    # ------------------------------------------------------------ 挂失 + 补卡换发
    # 唐小满(P1009) 的卡 A-1009 遗失：挂失，再用新卡补换发
    lost_card = "A-1009"
    s.report_lost(GUARD, lost_card, note="下班后发现卡套遗失，电话报失")
    s.replace_card(CLERK1, lost_card, stock[idx], note="补办新卡，旧卡注销")
    idx += 1

    # ------------------------------------------------------------ 损坏
    # 韩素(P1010) A-1010 折断损坏
    s.report_damaged(CLERK1, "A-1010", note="卡片折断，芯片无法读取")
    s.replace_card(CLERK1, "A-1010", stock[idx], note="损坏换卡")
    idx += 1

    # ------------------------------------------------------------ 离场回收（已完成）
    # 外包魏来(P1012)：历史发卡 → 离场冻结 → 回收
    d = s.create_issue_draft(CLERK2, stock[idx], 12, 5,
                             note="外包驻场临时卡")
    s.confirm_issue_draft(CLERK2, d["id"])
    wl_card = stock[idx]
    idx += 1
    s.depart_person(CLERK2, 12, note="驻场合同结束，办理离场")
    s.recycle_card(CLERK2, wl_card, note="离场当日交回卡片")

    # ------------------------------------------------------------ 离场冻结待回收
    # 罗振(P1011) 已离场，卡处于冻结待回收状态（演示用）
    d = s.create_issue_draft(CLERK1, stock[idx], 11, 2,
                             note="产品经理入职发卡")
    s.confirm_issue_draft(CLERK1, d["id"])
    frozen_card = stock[idx]
    idx += 1
    s.depart_person(CLERK1, 11, note="离职交接，门禁卡尚未交回")
    # frozen_card 保持 frozen —— 待回收清单

    # ------------------------------------------------------------ 借用转交
    # 程远(P1003) 把卡借给加班的林晓彤(P1002) 用于周末 3 层
    s.borrow_card(CLERK1, "A-1003", 2,
                  due_at=(datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d 20:00"),
                  note="周末加班临时借用，权限随卡不随人")

    # ------------------------------------------------------------ 临时卡
    # 1) 已到期失效的临时卡（历史状态，直接落库，保持演示直观）
    with db.tx() as conn:
        row = conn.execute(
            "SELECT id FROM cards WHERE card_no='T-2001'").fetchone()
        tid = row[0]
        conn.execute(
            "UPDATE cards SET status='expired', person_id=5, group_id=5,"
            " valid_from='2026-09-10', expires_at='2026-09-15 18:00',"
            " issued_at='2026-09-10 09:00:00', activated_at='2026-09-10 09:00:00'"
            " WHERE id=?", (tid,))
        conn.execute(
            "INSERT INTO lifecycle_records(card_id,card_no,action,person_id,"
            "operator_id,detail,created_at) VALUES(?,?, 'expire',5,?,"
            "'临时卡到期自动失效（到期 2026-09-15 18:00）',?)",
            (tid, "T-2001", None, "2026-09-15 18:00:00"))
        conn.execute(
            "INSERT INTO events(type,severity,card_id,card_no,person_id,detail,"
            "created_at) VALUES('temp_expired','warning',?, 'T-2001',5,"
            "'临时卡到期自动失效','2026-09-15 18:00:00')", (tid,))

    # 2) 即将到期的临时卡（启动后约 30 秒由进程内任务自动失效）
    soon = (datetime.now() + timedelta(seconds=30)).strftime("%Y-%m-%d %H:%M:%S")
    d = s.create_issue_draft(CLERK2, "T-2002", 6, 5, card_type="temporary",
                             expires_at=soon, note="访客临时卡，30 秒后到期演示")
    s.confirm_issue_draft(CLERK2, d["id"])

    # ------------------------------------------------------------ 设备离线缓冲
    # B 座 D-03 离线期间的两条待补传指令 + 一条离线刷卡记录
    with db.tx() as conn:
        for card_no, command in (("T-2003", "issue"), ("T-2004", "issue")):
            conn.execute(
                "INSERT INTO device_outbox(device_id,card_id,card_no,command,"
                "payload_json,status,created_at) VALUES(3,NULL,?,?,"
                "?, 'pending',?)",
                (card_no, command,
                 f'{{"card_no": "{card_no}", "command": "{command}"}}', now_iso()))
        conn.execute(
            "INSERT INTO access_events(device_id,card_no,floor,event_at,result,"
            "reason,synced,created_at) VALUES(3,'A-1005',4,?,'denied',"
            "'楼层 4 不在授权范围 [1, 5]（设备离线，本地记录待补传）',0,?)",
            (now_iso(), now_iso()))
        conn.execute(
            "INSERT INTO events(type,severity,device_id,detail,created_at)"
            " VALUES('device_offline','warning',3,'B座南门闸机网络中断，进入本地缓冲',?)",
            (now_iso(),))

    # ------------------------------------------------------------ 一次已确认的权限变更（历史）
    change = s.create_permission_change(
        ADMIN, 2, time_end="20:30",
        reason="研发攻坚期申请延长晚间通行半小时")
    s.confirm_permission_change(ADMIN, change["id"], True)
    # 再留一条待确认变更，供界面演示二次确认
    s.create_permission_change(
        ADMIN, 3, floors=[1, 5, 6],
        reason="云栖设计申请 6 层露台拍摄权限（待二次确认）")

    # ------------------------------------------------------------ 越界刷卡事件（在线设备）
    s.attempt_access(GUARD, 2, "A-1005", 3)   # 沈知微(云栖)刷三层 → 拒绝
    s.attempt_access(GUARD, 1, "A-1002", 1)   # 正常放行
    s.attempt_access(GUARD, 1, "A-1009", 3)   # 挂失卡 → 拒绝

    print("种子数据完成：%d 名人员、%d 张卡片、%d 个权限组"
          % (db.conn.execute("SELECT COUNT(*) FROM people").fetchone()[0],
             db.conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0],
             db.conn.execute("SELECT COUNT(*) FROM permission_groups").fetchone()[0]))
    print("演示看点：A-1009 挂失已补 %s；%s 冻结待回收；D-03 离线待补传；"
          "T-2002 将在约 30 秒后自动到期。"
          % (mask_card_no(stock[8] if len(stock) > 8 else "新卡"),
             mask_card_no(frozen_card)))
