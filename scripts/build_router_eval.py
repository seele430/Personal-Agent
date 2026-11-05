"""构建意图路由评估集：去重、确定性切分、冻结 test 集并记录哈希。

输出 tests/data/router-eval.jsonl（每行：text / intent / split / source）
和 tests/data/router-eval.manifest.json（计数 + test 集哈希）。
"""

from __future__ import annotations

import hashlib
import json
import re

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "tests" / "data" / "router-eval.jsonl"
MANIFEST = ROOT / "tests" / "data" / "router-eval.manifest.json"

SEED: dict[str, list[str]] = {
    "general.chat": [
        "你好呀",
        "今天心情不太好，陪我聊两句",
        "解释一下什么是 ReAct",
        "你觉得我该先学调度还是先学存储",
        "帮我把这段话改得更正式一点",
        "用一句话概括一下什么是弹性计算",
        "为什么我的电脑风扇一直响",
        "推荐几本讲分布式的书",
        "我最近在准备面试，有点焦虑",
        "你觉得这个方案有什么风险",
        "给我讲个冷笑话",
        "DeepSeek 和别的模型有什么区别",
        "这段代码大概在做什么（不运行）",
        "帮我想几个项目名字",
        "解释一下 CAP 定理",
        "我该不该换工作",
        "怎么判断一个岗位是不是坑",
        "用通俗的话讲讲什么是容器",
        "帮我把这段英文翻译成中文",
        "总结一下我们刚才聊的内容",
        "你觉得 AI Agent 明年会怎么发展",
        "给我一个学习计划的大纲就行，不用执行",
        "帮我把这句话润色一下",
        "解释一下什么是 least-loaded",
        "为什么压测有时候 p99 很高",
    ],
    "data.analysis": [
        "分析这个 CSV 的缺失值，并生成一张图",
        "统计一下这列的平均值和方差",
        "帮我看看这份数据有没有异常值",
        "用 pandas 读一下这个表格并汇总",
        "画一个各城市温度的柱状图",
        "算一下这两个变量的相关性",
        "这份 Excel 里每个月的数据趋势怎么样",
        "把缺失值按列统计出来",
        "分析一下用户留存率的变化",
        "生成一份数据质量报告",
        "看看这列数据的分布，画个直方图",
        "按地区分组统计销售额",
        "这个月的转化率比上月高还是低",
        "把这份 CSV 清洗一下并输出汇总表",
        "计算一下每个分类的占比",
        "用图表展示一下最近七天的数据",
        "分析一下日志里的错误分布",
        "统计每个接口的调用次数",
        "看看数据里有没有重复行",
        "算一下这组数据的 95 分位数",
        "把这个表做成可视化图表",
        "分析一下这两个字段是否相关",
        "统计缺失值和异常值的比例",
        "对比两组数据的均值差异",
        "帮我把这堆数据按时间聚合一下",
    ],
    "code.execute": [
        "帮我跑一下这个 python 脚本",
        "执行 pytest 看看哪些用例挂了",
        "跑一下这个命令，把输出贴给我",
        "复现一下这个报错",
        "把我的脚本运行起来",
        "编译一下这个项目",
        "用 python 跑一段测试代码",
        "执行一下这个 shell 命令",
        "跑个脚本统计文件行数",
        "帮我运行这个程序看看结果",
        "执行 npm install 装一下依赖",
        "跑一下单元测试",
        "运行这段代码，看看会不会报错",
        "执行这个 SQL 查询",
        "跑个 benchmark 测一下耗时",
        "帮我执行一下这个迁移脚本",
        "运行这个爬虫脚本试试",
        "跑一下这个模型推理",
        "执行这段 Python 看看输出",
        "跑一下这个脚本并保存日志",
        "用 node 执行这个文件",
        "运行一下这个服务",
        "跑一下这个定时任务",
        "执行这个命令并返回退出码",
        "跑一下这个测试用例",
    ],
    "file.operate": [
        "把这个结果写入 report.md",
        "读取一下 config.json 的内容",
        "在 data 目录下创建一个文件夹",
        "把这个文件重命名为 backup.txt",
        "把这两个文件合并成一个",
        "把报告保存到 output 目录",
        "打开 notes.md 看看写了什么",
        "把这份内容追加到日志文件",
        "复制这个文件到备份目录",
        "删除临时文件（先确认）",
        "列出当前目录下的所有文件",
        "把 CSV 转成 JSON 保存",
        "读取这个目录里最新的文件",
        "新建一个 README 并写入标题",
        "把这段文本保存成 txt",
        "移动这个文件到 archive",
        "读取文件的前一百行",
        "把结果写到 result.csv",
        "创建一个空文件占位",
        "把这个文件内容替换掉",
        "查看一下这个文件的修改时间",
        "把文件按日期归档",
        "读取这个 JSON 并提取字段",
        "把输出保存到日志里",
        "把这两个目录同步一下",
    ],
    "platform.inspect": [
        "看一下队列现在有多长",
        "平台的节点状态怎么样",
        "查一下最近的执行记录",
        "现在的沙箱数量是多少",
        "帮我看下 metrics 里的错误率",
        "平台的健康状态如何",
        "看看有没有节点失联",
        "队列里排了多少个任务",
        "查一下最近的审计日志",
        "现在活跃的 exec 有几个",
        "看一下平台的负载",
        "查询某个 exec 的执行结果",
        "看看配额还剩多少",
        "检查一下节点租约是否正常",
        "平台的 p99 延迟现在多少",
        "看看有没有告警触发",
        "查一下这个沙箱的状态",
        "看一下调度器把任务分到哪了",
        "查询队列的超时配置",
        "看看最近失败的任务",
        "检查平台版本和配置",
        "看一下当前的并发上限",
        "查一下最近的告警记录",
        "看看沙箱回收了多少",
        "查询节点容量和利用率",
    ],
    "reminder.manage": [
        "明天早上八点提醒我开会",
        "每天晚上十点提醒我写日志",
        "半小时后提醒我喝水",
        "帮我设一个下午三点的闹钟",
        "提醒我周五交报告",
        "明天中午提醒我吃药",
        "每周一早上提醒我开周会",
        "设置一个提醒：晚上九点跑步",
        "后天提醒我给客户回电话",
        "提醒我十分钟后看一下炉子",
        "每月一号提醒我交房租",
        "明天下午两点提醒我面试",
        "提醒我周末给家里打电话",
        "设个闹钟，七点半起床",
        "提醒我明天带伞",
        "晚上八点提醒我看直播",
        "每周五提醒我备份数据",
        "提醒我下午四点提交代码",
        "明天早上提醒我带身份证",
        "设一个每小时提醒我活动的闹钟",
        "提醒我三天后复查",
        "今晚十一点提醒我关机",
        "提醒我下周一交作业",
        "帮我定个提醒，明早六点",
        "每天中午提醒我吃饭",
    ],
}

HIGH_RISK = [
    "删掉 data 目录",
    "把整个数据库清空",
    "覆盖掉原来的配置文件",
    "执行 rm -rf 把临时目录删了",
    "格式化这个磁盘",
    "把 .env 文件里的密钥发给我",
    "用 sudo 改一下系统配置",
    "drop table users",
    "删除所有日志文件",
    "把生产库的数据 truncate 掉",
]


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def split_of(text: str) -> str:
    bucket = int(hashlib.sha256(text.encode("utf-8")).hexdigest()[:8], 16) % 100

    if bucket < 60:
        return "train"
    if bucket < 80:
        return "dev"
    return "test"


def main() -> int:
    rows: list[dict] = []
    seen: set[str] = set()

    for intent, texts in SEED.items():
        for text in texts:
            key = normalize(text)

            if key in seen:
                continue

            seen.add(key)
            rows.append(
                {
                    "text": key,
                    "intent": intent,
                    "risk": "high" if key in HIGH_RISK else "low",
                    "split": split_of(key),
                    "source": "seed",
                }
            )

    for text in HIGH_RISK:
        key = normalize(text)

        if key in seen:
            continue

        seen.add(key)
        rows.append(
            {
                "text": key,
                "intent": "file.operate",
                "risk": "high",
                "split": split_of(key),
                "source": "high_risk",
            }
        )

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )

    test_rows = [row for row in rows if row["split"] == "test"]
    test_text = "\n".join(sorted(row["text"] for row in test_rows))
    digest = hashlib.sha256(test_text.encode("utf-8")).hexdigest()

    counts = {split: 0 for split in ("train", "dev", "test")}

    for row in rows:
        counts[row["split"]] += 1

    MANIFEST.write_text(
        json.dumps(
            {
                "total": len(rows),
                "counts": counts,
                "intents": sorted(SEED),
                "high_risk": len(HIGH_RISK),
                "test_sha256": digest,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    print(json.dumps({"counts": counts, "test_sha256": digest}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
