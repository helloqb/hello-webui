# hello-webui

基于 Open WebUI 的自托管聊天界面，包含 DeepSeek 原生模型入口。当前基线为 Open WebUI `0.13.4`（`0666e59d`）。DeepSeek 负责搜索、网页阅读、深度思考与回答生成，Open WebUI 管理登录、会话和附件。 [原生模式开发与验收说明](docs/deepseek-native.md) · [Open WebUI 简体中文介绍](README.zh.md)

## 发布镜像

GitHub Actions 根据 `native-上游主版本.次版本.补丁版本.自定义序号` 标签构建普通 CPU 镜像，并推送到 `ghcr.io/helloqb/hello-webui`。工作流使用 GitHub 提供的 `GITHUB_TOKEN`。Fork 的日常推送仍可运行检查，Open WebUI 官方发布任务只在上游仓库执行。 [发布工作流](.github/workflows/native-image.yaml) · [GitHub 容器镜像发布文档](https://docs.github.com/en/actions/tutorials/publish-packages/publish-docker-images)

发布前在迷你主机上执行 `uname -m`。`x86_64` 使用默认的 `linux/amd64`。如果目标主机是 `aarch64`，请在打标签前设置仓库变量。

```sh
gh variable set NATIVE_IMAGE_PLATFORM --repo helloqb/hello-webui --body linux/arm64
```

确认工作区改动已提交且对应版本通过验收，再从仓库根目录发布。以下标签是首个发布版本的示例。后续发布使用新的自定义序号。

```sh
git push origin main
git tag -a native-0.13.4.1 -m 'hello-webui 0.13.4 native.1'
git push origin refs/tags/native-0.13.4.1
gh run list --repo helloqb/hello-webui --workflow native-image.yaml --event push --limit 5
gh run watch --repo helloqb/hello-webui
```

工作流成功后，镜像标签为 `ghcr.io/helloqb/hello-webui:native-0.13.4.1`，镜像摘要可在该次运行的摘要中查看。手工构建也可使用仓库的 `Dockerfile`，本地 `.webui_secret_key` 和部署目录已从构建上下文排除。

## 迷你主机部署

在装有 Docker Engine 和 Compose 插件的 Linux 主机上，将 [deploy/compose.yaml](deploy/compose.yaml) 放到 `/srv/hello-webui/compose.yaml`。Compose 启动 `webui` 和 `cloudflared`，将 SQLite、附件、配置及缓存写入 `hello-webui-data` 数据卷。WebUI 通过 Compose 网络接受 Tunnel 转发，路由器可以保持入站端口关闭。 [Compose 网络说明](https://docs.docker.com/compose/how-tos/networking/)

在同目录创建权限为 `600` 的 `.env`，按实际值填写。新实例可用 `openssl rand -hex 32` 生成 `WEBUI_SECRET_KEY`，以后保留同一个值。迁移已有实例时沿用该实例的密钥。Tunnel Token 与 Open WebUI 密钥只存放在主机部署目录和受控备份中。

```dotenv
WEBUI_IMAGE=ghcr.io/helloqb/hello-webui:native-0.13.4.1
CLOUDFLARED_IMAGE=cloudflare/cloudflared:2026.11.1
WEBUI_URL=https://chat.example.com
WEBUI_SECRET_KEY=<实例密钥>
TUNNEL_TOKEN=<远程管理的 Tunnel Token>
```

在 Cloudflare 为 `chat.example.com` 建立 Access 应用和允许名单，再配置远程管理的 Tunnel 路由，将该域名指向 `http://webui:8080`。启用 **Protect with Access** 并确认域名支持 WebSocket。将域名填入 `WEBUI_URL`，Compose 会用它设置浏览器访问来源。已有数据库上的 WebUI URL 可以在管理员设置中更新。 [Cloudflare Access 配置](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/self-hosted-public-app/) · [Tunnel 参数](https://developers.cloudflare.com/tunnel/reference/run-parameters/#token) · [Open WebUI 配置](https://docs.openwebui.com/reference/env-configuration/#webui_url)

如果 GHCR 包为私有，先在迷你主机用具有 `read:packages` 权限的个人访问令牌登录 GHCR。运行 `docker login ghcr.io --username helloqb` 后，在密码提示处输入令牌。 [GHCR 身份验证](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry#authenticating-to-the-container-registry)

```sh
cd /srv/hello-webui
docker compose config --quiet
docker compose pull
docker compose up -d --wait
docker compose ps
```

通过配置好的 HTTPS 域名进入 WebUI。首次创建管理员后，在「管理员面板 → 设置 → 连接」添加 **DeepSeek 原生**，填入 DeepSeek API Key。根据需要配置模型访问权限。用外网验证登录、联网搜索、流式回答、停止生成、附件和重开会话。若要迁移开发机数据，先用数据副本检查附件路径，旧文件记录可能引用开发机的绝对路径。 [DeepSeek 原生配置与验收](docs/deepseek-native.md)

## 同步上游、升级与回滚

`origin` 指向自定义仓库，`upstream` 指向 Open WebUI 官方仓库。首次尚未配置 `upstream` 时执行 `git remote add upstream https://github.com/open-webui/open-webui.git`。升级时先拉取官方标签，在升级分支合入选定版本，处理冲突并验证前端构建和原生功能。在独立数据副本上验收数据库迁移之后，将升级合入 `main` 并发布新的 `native-*` 标签。

生产升级前先记下旧镜像引用，停止 WebUI，再完整备份数据卷。妥善保管部署 `.env` 的独立备份。以下命令在 `/srv/hello-webui` 执行，归档文件权限由 `umask` 限制。

```sh
mkdir -p backups
docker compose stop webui
(
  umask 077
  docker compose run --rm --no-deps -T --entrypoint tar webui \
    -C /app/backend/data -czf - . > "backups/data-$(date +%Y%m%d-%H%M%S).tar.gz"
)
```

确认备份成功后，在 `.env` 修改 `WEBUI_IMAGE` 为新版本，再执行 `docker compose pull webui` 和 `docker compose up -d --wait webui`。若备份失败，用原版本重新启动 WebUI。

如果新版本未执行数据库迁移，可切回旧镜像并重新启动。如果新版本已迁移数据库，将升级前的归档解包到新的 Docker 卷，同时将 `.env` 中的 `WEBUI_DATA_VOLUME` 和 `WEBUI_IMAGE` 指向恢复卷及旧镜像，再启动 WebUI。保留故障版本的数据卷，便于处理升级后产生的数据。 [Open WebUI 更新与回滚](https://docs.openwebui.com/getting-started/updating/)
