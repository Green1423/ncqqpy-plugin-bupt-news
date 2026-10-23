# 五、作用域（命令能在哪里用 / 影响什么）

> 文档索引：[docs/README.md](https://github.com/Green1423/ncqqpy/blob/main/docs/README.md) · 上级：[bupt_news 插件](README.md) · 上一章：[命令](04-commands.md) · 下一章：[工作流程](06-workflow.md)


「作用域」在本项目里有三层，含义完全不同：

## 5.1 命令的 `scope` 参数 —— 子命令各不相同

| `scope` | 命令 |
|---------|------|
| `private`（仅私聊） | `status` `get` `targets` `add` `del` `on` `off` `interval` `images` `bind` |
| `all`（私聊 + 群聊） | 父命令 `buptnews`（命令组本体）、`list` `sync` |

> `scope` 只管「在私聊还是群里」，**不回答「这个私聊能不能用」** —— 后者是 (04-commands.md#42-私聊可用范围binding-两道闸门别混为一谈) 的
> `binding`（服务范围），判定发生在权限校验**之前**。

框架在 `CommandManager.handle()` 里**按实际执行的那条命令（子命令优先）**判定：

```python
if not effective.allows_context(is_group):      return ("_not_found", cmd)

# CommandHandler.allows_context
if self.scope == "private" and is_group:     return False
if self.scope == "group"   and not is_group: return False
```

> `handle()` 先解析「实际执行的 handler」（子命令优先）再按它自己的 `scope`/`permission`
> 校验，`/help` 用同一条 `allows_context()` 判定，两者不会分叉（见 (../../guide/06-commands.md#65-子命令)）。
> 例外：**带子命令的命令组本体不校验权限**（权限属于子命令），但本体的 `scope` 仍然生效。

## 5.2 真正决定「机器人听哪个群」的是 `bound_groups`

⚠️ 真正卡住群聊的是**框架入口的事件过滤**，比 `scope` 严格得多。
`napcat/client.py` 的 `_on_ws_event()`：

```python
if (event.message_type == "group"
        and event.group_id is not None
        and not self.groups.is_bound(event.group_id)):
    return                       # 事件在入口被丢弃，根本进不了 EventBus
```

当前 `config.json` 是 `bound_groups: [1000000001]`、`all_groups: false`，实测：

| 群号 | `is_bound` |
|------|------------|
| `1000000001` | ✅ True |
| 其它任意群 | ❌ False |

也就是说：**只有 1000000001 这个群的任何事件会被处理**，其它群的消息、入群、
禁言、戳一戳等全部在入口丢弃。**私聊永远不受影响**（代码只对 `group` 判定）。

> 🪤 **陷阱**：`config.json` 里的 `all_groups` 在 `bound_groups` 非空时会被**忽略**：
> ```python
> all_groups=all_groups if bound_groups is None else not bool(bound_groups)
> ```
> 所以想「监听所有群」必须把 `bound_groups` **清空**（`[]`），
> 单把 `all_groups` 改成 `true` 是没用的。

## 5.3 命令影响的「数据作用域」是全局的

`groups` / `users` / `interval` / `enabled` / `columns` 都存在
`plugins.bupt_news.*` 下，是**全局配置，不区分群**：

- `/buptnews add group X` 改的是 Bot 的全局广播列表，在哪个群执行都一样；
- 定时同步没有会话上下文，会把新通知发给**所有**已配置目标。

框架本身支持按群隔离配置（`self.config.group(gid)`，`essential` 插件的欢迎语就是这么做的），
但本插件的推送目标用的是全局配置。若希望「每个群各自订阅、互不影响」，
需改用 `self.config.group(gid)` 存目标，并给命令加上 `scope="group"`。

---
