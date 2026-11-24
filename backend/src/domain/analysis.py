from collections import defaultdict
from math import erfc, sqrt


COLUMNS = (
    ("process", "归类工序"), ("grade", "损伤等级"), ("count", "完成件数"),
    ("long", "延期 2 天以上"), ("pct", "长延期率（%）"), ("gap", "高低组长延期率差（百分点）"),
    ("z", "正向趋势 Z 值"), ("p", "趋势单侧 p 值"), ("decision", "排期判读"),
)


def validate(row):
    if row["severity"] not in range(1,5) or row["delay"] not in range(1,5) or row["count"] <= 0:
        raise ValueError("损伤等级、延期等级须为 1–4，数量必须为正")


def ordered_trend(grades):
    total = sum(count for _,count,_ in grades)
    delayed = sum(late for _,_,late in grades)
    if len(grades) < 2 or total <= 1 or delayed in (0,total):
        return None, None, False
    baseline = delayed/total
    center = sum(grade*count for grade,count,_ in grades)/total
    numerator = sum(grade*(late-count*baseline) for grade,count,late in grades)
    variance = baseline*(1-baseline)*total/(total-1)*sum(count*(grade-center)**2 for grade,count,_ in grades)
    z = numerator/sqrt(variance)
    expected = min(min(baseline*count,(1-baseline)*count) for _,count,_ in grades)
    return z,erfc(z/sqrt(2))/2,expected >= 5


def analyze(rows, parameters):
    alpha = parameters.get("alpha",0.05)
    boundary = parameters.get("review_gap",15)
    groups = defaultdict(lambda: defaultdict(lambda: [0,0]))
    seen = set()
    for row in rows:
        validate(row)
        key = row["process"],row["sheet"]
        if key in seen:
            raise ValueError(f"损伤台账 {row['sheet']} 有重复汇总键")
        seen.add(key)
        counts = groups[row["process"]][row["severity"]]
        counts[0] += row["count"]
        if row["delay"] >= 3:
            counts[1] += row["count"]
    if not groups:
        raise ValueError("至少录入一张延期等级汇总单")
    output, chart = [], []
    review = 0
    for process,counts in sorted(groups.items()):
        grades = [(grade,*counts[grade]) for grade in sorted(counts)]
        z,p,sufficient = ordered_trend(grades)
        low_n = sum(n for grade,n,_ in grades if grade < 3)
        high_n = sum(n for grade,n,_ in grades if grade >= 3)
        low_late = sum(late for grade,_,late in grades if grade < 3)
        high_late = sum(late for grade,_,late in grades if grade >= 3)
        gap = 100*(high_late/high_n-low_late/low_n) if low_n and high_n else None
        flagged = sufficient and p <= alpha and gap is not None and gap >= boundary
        review += flagged
        decision = "复查长预约档" if flagged else "稀疏或无变异，补采" if not sufficient else "保留现行排期"
        output.append(dict(process=process,count=low_n+high_n,long=low_late+high_late,
                           pct=round(100*(low_late+high_late)/(low_n+high_n),3),
                           gap=round(gap,3) if gap is not None else None,z=round(z,5) if z is not None else None,
                           p=round(p,8) if p is not None else None,decision=decision))
        output.extend(dict(process=process,grade=grade,count=n,long=late,pct=round(100*late/n,3)) for grade,n,late in grades)
        if gap is not None:
            chart.append((process,round(gap,3)))
    return report(f"{review} 个修补工序需复查较长预约档",
                  f"每个工序先核查等级与延误的正向趋势，显著线为 {alpha:g}；再核对 3–4 级减 1–2 级的长延期率是否达到 {boundary:g} 个百分点。",
                  [("交付件数",sum(row["count"] for row in rows),"件"),("工序",len(groups),"类"),("排期复查",review,"类")],
                  COLUMNS,output,"高低损伤组长延期率差",chart,"百分点")



def seed():
    grids = (
        ("植鞣皮补色",(18,5,2,0,12,14,6,3,6,10,17,7,1,5,9,24)),
        ("搭扣换簧",(13,4,2,1,14,4,2,1,14,5,1,1,15,5,2,1)),
        ("内衬拼缝",(8,3,1,0,9,10,11,8,4,9,12,14,2,2,15,24)),
    )
    return [dict(process=process,sheet=f"SC-{index+1:02d}-{cell+1:02d}",
                 severity=cell//4+1,delay=cell%4+1,count=count)
            for index,(process,grid) in enumerate(grids) for cell,count in enumerate(grid) if count]


def report(headline, summary, metrics, columns, rows, chart_label, chart_values, unit=""):
    return {
        "headline": headline, "summary": summary,
        "metrics": [{"label": label, "value": value, "unit": suffix} for label, value, suffix in metrics],
        "columns": [{"key": key, "label": label} for key, label in columns],
        "rows": [{key: row.get(key) for key, _ in columns} for row in rows],
        "chart": {"label": chart_label, "unit": unit,
                  "points": [{"label": label, "value": value} for label, value in chart_values]},
    }
