# Python Agent 第一周代码

配套教程：[第一周详细教程](../Python_Agent_第一周详细教程.md)。Python 3.11+；模拟模式只需标准库。

在本目录执行：

```bash
python3 main.py probe
python3 main.py compare
python3 main.py run
python3 main.py run --scenario repeat
python3 -m unittest -v test_week1
```

`repeat` 是故障演示，预期返回非零退出码。所有命令默认模拟模式；只有 `--real` 才调用 API。

真实模式需要：在独立虚拟环境安装 `requirements.txt`、设置 `OPENAI_API_KEY` 和 `OPENAI_MODEL`，并复制、填写 `config.local.json`。详细步骤、费用边界与逐日验收请阅读教程。

`logs/` 是运行轨迹，`artifacts/` 是实验配置和版本清单。仅使用公开、无敏感信息的练习题。默认模拟模型是固定脚本，不理解任意输入。

已验证离线控制逻辑；真实服务集成仍需按教程补验。没有发起付费请求。
