# Docker 安装代理转发

`proxy_forwarder.py` 使用 Python 标准库将 Docker 网桥可访问的端口转发到服务器回环代理，不保存凭据。

先在有代理的客户端保持 SSH 反向隧道（将 `my` 替换为自己的 SSH 别名）：

```bash
ssh -N -o ExitOnForwardFailure=yes -R 127.0.0.1:17890:127.0.0.1:7897 my
```

再在服务器启动转发器：

```bash
python scripts/proxy/proxy_forwarder.py --listen-host 172.17.0.1 --listen-port 17899 --target-host 127.0.0.1 --target-port 17890
```

Docker 网关、端口按实际环境调整。仅绑定容器网桥地址，避免暴露到公网。转发器默认目标端口为 10808；上述命令显式使用 SSH 隧道端口 17890。

SkillsBench 安装器通过 `bench.py --install-proxy http://172.17.0.1:17899` 使用此代理；API 网关由独立环境变量配置，详见 SkillsBench 脚本说明。退出隧道或转发器会中断代理连接。
