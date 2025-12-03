

# 复制服务文件到systemd用户目录
mkdir -p ~/.config/systemd/user
cp tradecopy.service ~/.config/systemd/user/

# 重新加载systemd配置
systemctl --user daemon-reload

# 启用并启动服务
systemctl --user enable tradecopy
systemctl --user start tradecopy

# 启用用户linger，确保用户登出后服务仍然运行
sudo loginctl enable-linger "$USER"


# 查看服务状态
systemctl --user status tradecopy

# 查看服务日志
journalctl --user -u tradecopy -f

# 停止服务
systemctl --user stop tradecopy

# 重启服务
systemctl --user restart tradecopy

# 修改代码，运行新代码
systemctl --user stop tradecopy && systemctl --user start tradecopy