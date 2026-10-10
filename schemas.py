"""bupt_news 插件的命令参数 Schema。

位置参数按字段定义顺序绑定，也可用 `key=value` 形式传入。
"""
from napcat import (
    BoolField,
    ChoiceField,
    CommandSchema,
    DurationField,
    IntField,
    QQField,
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
    id = IntField(required=True, min=1,
                  description="推送消息里的数字编号")


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
