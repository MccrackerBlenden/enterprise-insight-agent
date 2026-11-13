# 测试、构建与验收

## 自动回归

```bash
(cd backend && uv run --locked pytest -q)
(cd backend && uv run --locked python -m evals.run offline)
(cd frontend && npm ci --no-audit --no-fund && npm run test:unit && npm run check:visual && npm run build)
python3 -m unittest discover -s scripts/tests -v
python3 scripts/check_container_inputs.py
```

## 本机容器验证

```bash
cp -n backend/.env.example backend/.env
EIA_WEB_PORT=18080 docker compose -p eia-portfolio -f compose.yaml up -d --build --wait
python3 scripts/verify_visual_deployment.py --base-url http://127.0.0.1:18080
python3 scripts/smoke_local_docker.py --base-url http://127.0.0.1:18080
```

并在浏览器中实际检查决策总览、Agent 工作台、运营处置中心、任务历史、窄屏布局和三场景操作。

## 发布安全检查

```bash
# 每次提交与发布前执行：
python3 scripts/check_release_safety.py --require-locks
python3 scripts/check_history_paths.py
```

注意：启发式当前文件检查不能替代密钥、数据来源、素材许可的人工复核，也不能证明真实模型提供商兼容性或生产安全。
