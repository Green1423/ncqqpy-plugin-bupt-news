"""bupt_news 插件的命令参数 Schema。

位置参数按字段定义顺序绑定，也可用 `key=value` 形式传入。
"""
from napcat.command import (
    BoolField, ChoiceField, CommandSchema, DurationField, IntField, QQField, StrField,
    VariadicField,
)


class AddTargetSchema(CommandSchema):
    kind = ChoiceField(["group", "user"], default="group",
                       description="目标类型：group=群 / user=QQ用户")
    target = QQField(required=False,
                     description="群号或 QQ 号；群聊中省略则用当前群")


class DelTargetSchema(CommandSchema):
    kind = ChoiceField(["group", "user"], default="group",
                       description="目标类型：group=群 / user=QQ用户")
    target = QQField(required=False,
                     description="群号或 QQ 号；群聊中省略则用当前群")


class BindSchema(CommandSchema):
    """管理「私聊可用范围」白名单（判定逻辑见 main.py 的 check_access）。"""

    kind = ChoiceField(["user", "group"], default="user",
                       description="user=私聊白名单用户 / group=允许小窗的群")
    target = QQField(required=False,
                     description="QQ 号或群号；省略则只列白名单（不改动）")
    action = ChoiceField(["add", "del", "list"], default="add",
                         description="add=加入白名单 / del=移出 / list=只看不改")


class IntervalSchema(CommandSchema):
    interval = DurationField(default="10m", min=60, max=86400,
                             description="同步间隔，支持 600 / 10m / 1h")


class SyncSchema(CommandSchema):
    pages = IntField(default=1, min=1, max=10,
                     description="抓取页数，每页 20 条")
    preview = BoolField(default=False,
                        description="只私聊发给自己，不广播到推送目标")


class GetSchema(CommandSchema):
    # 既可以是本地编号（`476`），也可以是原文链接（门户检索结果没有本地编号）。
    # 所以是 StrField 而不是 IntField —— 用 IntField 会在传链接时直接校验失败。
    id = StrField(required=True, max_length=512,
                  description="通知编号（如 476）或原文链接")


class ListSchema(CommandSchema):
    page = IntField(default=1, min=1, max=100000,
                    description="页码，从 1 开始（最新在前）")


class ImagesSchema(CommandSchema):
    action = ChoiceField(["stats", "backfill", "download", "retry"], default="stats",
                         description="stats=看缓存统计 / backfill=重抓历史记录补图并下载 / "
                                     "download=只下载缺失的图 / retry=重试下载失败的图")
    limit = IntField(default=0, min=0, max=100000,
                     description="本次最多处理多少条（0=用配置的 recrawl_max / 下载上限）")
    id = IntField(default=0, min=0,
                  description="backfill 时只重抓这一条通知（0=按 limit 批量）")


class PinSchema(CommandSchema):
    # ⚠️ `ids` 必须排在 `force` **之前**：位置参数按字段顺序绑定，
    # 否则 `pin 4` 里的 `4` 会被绑到 force 上（报"必须为布尔值"）。
    # 框架不认"无值长选项"，所以 force 要写成 `--force=true` 或短选项 `-f`
    #（短选项在 kwargs 里的键是 `f`，命令层做了兼容）。
    ids = VariadicField(description="要置顶的通知编号，可多个（空格或逗号分隔）；"
                                    "不带参数=查看当前置顶")
    force = BoolField(default=False,
                      description="已到置顶上限时替换最早置顶的那条"
                                  "（不加则报错并提示先 unpin）。"
                                  "写法：`--force=true` 或短选项 `-f`")


class UnpinSchema(CommandSchema):
    ids = VariadicField(description="要取消置顶的通知编号，可多个；"
                                    "不带参数=取消全部置顶")


class SearchSchema(CommandSchema):
    # ⚠️ 字段顺序 = 位置参数顺序。`source`/`content` 必须排在
    # `limit`/`page`/`date` **之后**，否则 `/buptnews search 疫苗 5 1` 里的 5 会被
    # 当成 `source` 而报"无效选项"（可选参数按位置绑定，不是按名字跳着来）。
    keyword = StrField(required=True, max_length=64,
                       description="关键词（匹配标题/摘要/正文；门户侧见 content）")
    limit = IntField(default=10, min=1, max=50, description="每页条数")
    page = IntField(default=1, min=1, max=100000, description="页码")
    date = StrField(default="", max_length=10,
                    description="只看某天，格式 YYYY-MM-DD（仅本地搜索；可省略）")
    source = ChoiceField(["all", "local", "portal"], default="all",
                         description="all=本地+门户（默认）/ local=只搜本地库 / "
                                     "portal=只搜门户（含本地库里没有的旧通知）")
    content = BoolField(default=True,
                        description="搜门户时是否连正文一起搜（false=只搜标题）")
