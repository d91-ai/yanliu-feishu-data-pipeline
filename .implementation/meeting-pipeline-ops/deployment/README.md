# 常驻运行与漏事件补偿模板

本目录只包含可复用模板，不包含真实路径、凭据、Base/Drive 标识或审核人 ID。

1. 将三个 `.service.example` 复制到 `/etc/systemd/system/`，去掉 `.example` 后缀。
2. 把其中的 `/ABSOLUTE_REPO_PATH`、`/ABSOLUTE_RUNTIME_PATH` 和 `DEPLOY_USER` 替换为本机值。
3. 执行 `systemctl daemon-reload`，再对三个服务执行 `enable --now`。
4. Windows + WSL2 用户还应让 Windows 登录时启动该 WSL 发行版；可复制
   `start-wsl-pipeline.ps1.example`，填写发行版名称后，在任务计划程序中设置“用户登录时”运行。

Router 的 Compose 服务自身使用 `restart: unless-stopped`；Worker 和漏事件补偿器由
systemd 保持常驻。补偿器每五分钟扫描最近 48 小时内“已有附件但尚无会议ID”的记录，
并通过与正常事件相同的 Router 入口恢复处理。它不会读取或输出附件正文。

启用前先手工执行一次只读扫描：

```bash
python3 -B reconcile_missed_ingress.py \
  --router-env /ABSOLUTE_RUNTIME_PATH/router/.env.router \
  --route-env /ABSOLUTE_RUNTIME_PATH/router/.env.meeting-minutes \
  --once
```

确认候选数量合理后，再加 `--apply`。服务模板已经显式包含 `--apply`。
