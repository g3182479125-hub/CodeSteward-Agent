# ShopSphere 训练项目说明

这个目录是 DevPilot / nanobot 用来录制真实 Bug-fix 闭环的本地 ShopSphere
副本。它是 Flask + SQLite 商城，项目本身不是 DevPilot 控制面。

## 运行和测试

在本目录执行：

```powershell
python -m pip install -r requirements.txt
python -m unittest discover -s tests -t .
python run.py
```

当前修复版验收结果是 `223` 个测试全部通过。`run.py` 会在首次启动时创建
`instance/` 下的本地数据库和密钥；这些内容不属于 Git 提交。

## 本次已带入的修复

1. `shop/orders.py`：订单取消先用带原状态条件的单条 `UPDATE` 抢占状态，只有
   真正抢占成功的请求才能恢复库存，避免两个并发取消把库存加两次。
2. `shop/auth.py` 和两个模板：运行时执行副本会过滤文件名中含有 `password`
   的模板，因此密码重置页面改名为 `forgot_form.html` 和 `reset_form.html`，
   并同步修改渲染引用。否则限流逻辑本身虽然生效，页面会因为
   `TemplateNotFound` 返回 500。
3. `tests/test_shop.py`：加入并发取消订单的回归测试，验证两个请求中只能有
   一个恢复库存。

## 录制前的基线

这个目录已经是“修复后的快照”，不是原始故障基线。若要录制“先失败、再让
Agent 修复”的视频，请先把这个目录复制一份，在复制品中按交接文档恢复故障，
不要直接破坏主副本。若只是继续开发，直接从当前版本开始即可。

不要提交以下运行时内容：`instance/`、`.venv/`、`.nanobot/`、缓存、日志、
SQLite 数据库、上传文件和任何 API Key。
