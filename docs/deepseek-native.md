# DeepSeek 原生

此自定义版本基于 Open WebUI `0.13.4`，上游提交为 `0666e59d26a4d056e0bacee41742486badd71cf8`。原生入口通过服务端调用 DeepSeek Messages API，模型固定为 `deepseek-flash`。Open WebUI 继续管理账号、连接权限、历史记录和附件原件。 [DeepSeek Chatbot 指南](https://api-docs.deepseek.com/zh-cn/guides/build_a_chatbot/)

## 构建和启动

在包含原生入口改动的仓库根目录构建自定义镜像。项目 Dockerfile 使用 Node.js 22 和 Python 3.12。

```sh
docker build --build-arg BUILD_CHANNEL=deepseek-native -t hello-webui:deepseek-native-0.13.4 .
docker run -d --name hello-webui --restart unless-stopped \
  -p 127.0.0.1:3000:8080 \
  -v hello-webui-data:/app/backend/data \
  hello-webui:deepseek-native-0.13.4
```

浏览器打开 [本机的 Open WebUI](http://localhost:3000)。已有实例应保留原来的卷、数据库、环境变量和端口映射。新建实例可用以上命令中的独立数据卷。

如需直接从源码启动，在带 Node.js 22 和 `uv` 的环境中执行以下命令。先构建前端，再从工作区启动 Python 后端。

```sh
ONNXRUNTIME_NODE_INSTALL_CUDA=skip npm ci --force
npm run build
uv sync --locked --python 3.12 --no-install-project
DATA_DIR="$PWD/backend/data" \
FRONTEND_BUILD_DIR="$PWD/build" \
PYTHONPATH="$PWD/backend" \
  uv run --no-sync python -m uvicorn open_webui.main:app --host 127.0.0.1 --port 8080
```

源码启动时请沿用实例的 `WEBUI_SECRET_KEY`。新实例也应设置并持久保存自己的 `WEBUI_SECRET_KEY`。浏览器打开 [本机源码实例](http://localhost:8080)。

## 配置连接

1. 用管理员登录，在 **管理员面板 → 设置 → 连接** 的 OpenAI API 区域添加连接，将提供商设为 **DeepSeek 原生（DeepSeek Native）**。
2. 填入 DeepSeek 开放平台的 API Key。服务地址为 `https://api.deepseek.com`。用连接弹窗的验证功能检查 Key，然后保存。再次编辑连接时留空可沿用已保存在服务端的 Key，填写新值可轮换。可通过连接的模型 ID 前缀区分多个原生连接。
3. 在管理员的模型管理中为 `deepseek-native` 或带前缀的模型设置访问权限。用获授权的账户打开聊天，在模型选择器中选 **DeepSeek 原生**。按需切换输入框旁的 **DeepSeek 联网搜索**和**深度思考**。聊天请求由 Open WebUI 后端鉴权并发往 DeepSeek。

## 请求和上下文

- 始终声明 `deepseek_read_url`，搜索开关独立控制 `deepseek_web_search`。思考由 `thinking.type` 显式开启或关闭。搜索、页面打开和页内查找均由 DeepSeek 编排。思考与搜索同时打开时，DeepSeek 使用深度搜索流程，链接阅读不会作为独立步骤运行。 [联网工具指南](https://api-docs.deepseek.com/zh-cn/guides/web_tools/)
- 原生模式跳过 Open WebUI 的搜索、知识库检索、消息压缩、本地工具和请求及响应过滤器。显式聊天系统提示词和按权限开启的记忆可能进入请求。做对照测试时关闭记忆，从无自定义系统提示词和知识库的新对话开始。DeepSeek 的内容安全参数设为 `high`。 [构建 Chatbot](https://api-docs.deepseek.com/zh-cn/guides/build_a_chatbot/)
- Open WebUI 保存回答、来源及 DeepSeek 返回的原始内容块。多轮请求原样回传同模型的正文、思考和原生工具块，包括链接阅读结果及签名。支持重新打开对话和继续追问。原生回复中止后可从该轮重新生成。正常原生聊天使用本地会话标题。
- 每次 Messages 请求默认最多等待 600 秒，连接阶段最多 30 秒，流式响应空闲最多 120 秒。可通过 `AIOHTTP_CLIENT_TIMEOUT` 和 `AIOHTTP_CLIENT_STREAM_IDLE_TIMEOUT` 配置总超时和流式空闲超时。每次附件上传及解析最多等待 300 秒。
- 普通会话从 Open WebUI 的原件存储和权限读取附件。图片、PDF、Word、PowerPoint 及 DeepSeek 支持的文本、代码、表格和电子书由后端上传至 DeepSeek Files API。文本和表格使用 `user_data`，PDF 和 Office 版式文档使用 `rich_layout_recognition`。DeepSeek 端副本有效期为 30 天，应用进程短期复用文件引用。进程重启、缓存淘汰或副本到期后，后续追问可能重新上传并产生文档解析费用。临时聊天沿用 Open WebUI 的浏览器文本提取和内联图片。 [Files API](https://api-docs.deepseek.com/zh-cn/guides/files_api/) · [文件解析](https://api-docs.deepseek.com/zh-cn/guides/document_parsing/)

## 验收

使用同一组输入对照 DeepSeek Messages 直调和原生聊天。核对 `model`、`thinking`、`tools`、消息顺序和附件用法，以及响应里的原生工具调用、结果、思考片段、文本及来源。搜索和阅读由模型按需决定，使用确实需要实时资料或指定网页内容的输入。

| 场景 | 预期 |
| --- | --- |
| 两个开关均关闭 | `thinking.type=disabled`，仅声明链接阅读，可返回普通回答 |
| 仅深度思考 | `thinking.type=enabled`，可展开思考内容 |
| 仅联网搜索 | 两个原生工具声明齐全，搜索由 DeepSeek 调用，显示进度和返回的来源 |
| 搜索加思考 | 可出现深度搜索、打开页面和页内查找，引用与原生返回一致 |
| 链接阅读与追问 | 关闭搜索，发送可访问的公开 HTTPS 网页。取得阅读结果后刷新页面或从另一设备重新打开会话，再追问页面内容，检查签名随原始内容重放 |
| 附件与追问 | 上传一份 PDF 和一张图片，检查后台 Files API 文件引用、模型回答及重开后的附件上下文 |
| 停止和错误 | 停止正在生成的回答，检查进度结束和部分回答。用测试连接验证无效 Key、超时或服务错误会显示可读提示 |
| 登录隔离 | 用无授权的账号验证连接不可用、无法读他人的私有会话及原件。检查普通浏览器会话的请求及响应里没有 DeepSeek Key |

可用本地协议测试模拟上游事件和附件上传。依赖安装后在仓库根目录运行，测试使用独立的临时数据库和测试密钥。

```sh
DEEPSEEK_TEST_DATA_DIR=$(mktemp -d)
DATA_DIR="$DEEPSEEK_TEST_DATA_DIR" \
DATABASE_URL="sqlite:///$DEEPSEEK_TEST_DATA_DIR/webui.db" \
WEBUI_SECRET_KEY=deepseek-native-unit-test-local-secret \
PYTHONPATH="$PWD/backend" \
  uv run --no-sync python -m unittest discover -s backend/open_webui/test -p 'test_deepseek*.py'
```

## 升级与回滚

1. 保存当前自定义源码和镜像标签。在变更实例前备份数据目录或持久卷、外部数据库和文件存储，并保管现有 `WEBUI_SECRET_KEY`、连接与部署配置。
2. 将自定义改动应用到经选定的下一版 Open WebUI 基线。运行前端构建和上述协议测试，使用独立测试实例复查连接权限、附件与刷新后追问。
3. 用新标签构建镜像，停止原容器后按原部署配置启动新镜像。保留同一数据卷及数据库连接，核对已有聊天和原生模型入口。源码部署采用相同的备份和验证步骤。
4. 如需回滚，停止升级后的实例并恢复先前镜像或源码。若启动时执行了数据库迁移，同时恢复与先前版本对应的数据备份。
