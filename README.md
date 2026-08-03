
<div align="center">

![:name](https://count.getloli.com/@astrbot_plugin_parser?name=astrbot_plugin_parser&theme=minecraft&padding=6&offset=0&align=top&scale=1&pixelated=1&darkmode=auto)

# astrbot_plugin_parser

_✨ 链接解析器 ✨_  

[![License](https://img.shields.io/badge/License-MIT-green.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![AstrBot](https://img.shields.io/badge/AstrBot-3.4%2B-orange.svg)](https://github.com/Soulter/AstrBot)
[![GitHub](https://img.shields.io/badge/作者-Zhalslar-blue)](https://github.com/Zhalslar)

</div>

## 📖 介绍

当前支持的平台和类型：

| 平台    | 触发的消息形态                    | 视频 | 图集 | 音频 |
| ------- | --------------------------------- | ---- | ---- | ---- |
| B 站    | av 号/BV 号/链接/短链/卡片/小程序 | ✅​  | ✅​  | ✅​  |
| 抖音    | 链接(分享链接，兼容电脑端链接)    | ✅​  | ✅​  | ❌️  |
| 微博    | 链接(博文，视频，show, 文章)      | ✅​  | ✅​  | ❌️  |
| 小红书  | 链接(含短链)/卡片                 | ✅​  | ✅​  | ❌️  |
| 小黑盒  | 链接/卡片                         | ✅​  | ✅​  | ❌️  |
| 知乎    | 链接/卡片                         | ✅​  | ✅​  | ❌️  |
| 快手    | 链接(包含标准链接和短链)          | ✅​  | ✅​  | ❌️  |
| 微信视频号 | 链接(含短链)                   | ✅​  | ✅​  | ❌️  |
| acfun   | 链接                              | ✅​  | ❌️  | ❌️  |
| youtube | 链接(含短链)                      | ✅​  | ❌️  | ✅​  |
| tiktok  | 链接                              | ✅​  | ❌️  | ❌️  |
| instagram | 链接                            | ✅​  | ✅​  | ❌️  |
| twitter | 链接                              | ✅​  | ✅​  | ❌️  |
| Iwara | 链接                              | ✅​  | ✅​  | ❌️  |
| Pixiv | 链接 / pid                         | ✅​  | ✅​  | ❌️  |

本插件目标：凡是链接皆可解析！尽请期待更新（如果可以,请提交PR）

---

## 🎨 效果图

插件默认启用 PIL 实现的通用媒体卡片渲染，效果图如下

<div align="center">

<img src="https://raw.githubusercontent.com/fllesser/nonebot-plugin-parser/refs/heads/resources/resources/renderdamine/video.png" width="160" />
<img src="https://raw.githubusercontent.com/fllesser/nonebot-plugin-parser/refs/heads/resources/resources/renderdamine/9_pic.png" width="160" />
<img src="https://raw.githubusercontent.com/fllesser/nonebot-plugin-parser/refs/heads/resources/resources/renderdamine/4_pic.png" width="160" />
<img src="https://raw.githubusercontent.com/fllesser/nonebot-plugin-parser/refs/heads/resources/resources/renderdamine/repost_video.png" width="160" />
<img src="https://raw.githubusercontent.com/fllesser/nonebot-plugin-parser/refs/heads/resources/resources/renderdamine/repost_2_pic.png" width="160" />

</div>

---

## 💿 安装

直接在astrbot的插件市场搜索astrbot_plugin_parser，点击安装，等待完成即可

## ⚙️ 配置

请在astrbot的插件配置面板查看并修改

---

## 🌐 跨主机临时 HTTP 媒体发送模式

默认情况下（`media_send_mode = local`），插件把下载的媒体以 `file://` 本地路径
发送，适用于 NapCat 等与插件运行在**同一主机**的部署。

当 NapCat 与插件**不在同一主机**、无法访问插件本机路径时，可切换为
`http` 模式：插件为缓存文件生成指向外部静态服务的临时 HTTP(S) URL 再发送。
**注意：必须自行部署外部静态服务指向插件的 cache 目录，否则媒体将无法被
NapCat 下载。**

### 配置项

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `media_send_mode` | string | `local` | `local`：`file://` 本地路径发送（默认，行为不变）；`http`：临时 HTTP(S) URL 发送 |
| `media_http_base_url` | string | 空 | HTTP 模式下必填。外部服务对外的基础地址，如 `https://media.example.com/astrbot`。仅允许 http/https 且必须包含主机名，请勿携带查询参数 |
| `media_http_ttl` | int | `3600` | HTTP 模式下缓存文件至少保留的秒数，过期后由清理任务按修改时间删除。建议与外部服务/CDN 缓存时长一致；留 0 或非法值回退默认值 |

插件按 `base_url + 相对 cache 目录路径` 拼接媒体 URL，每个路径段都会做
URL 编码（空格、中文等字符可正确发送），且只允许访问 cache 目录内的文件。

### 部署建议：主 Caddy + 独立只读静态容器

插件**只负责 URL 映射与 TTL 清理**，不负责 TLS、IP 访问控制或静态文件服务
本身。推荐采用「主 Caddy 负责入口与 TLS，独立 `nginx-unprivileged` 只读
静态容器负责挂载并对外提供 cache 目录」的分工：

- 主 Caddy：作为反向代理 / TLS 入口，只做请求转发与证书终止，**不直接挂载
  cache 目录**。
- 独立静态容器：使用 `nginx-unprivileged` 镜像，以**只读**方式挂载插件的
  cache 目录，仅暴露在受控网络（内网 / IP ACL）中，供主 Caddy 转发。

下面是一个示意性的 `docker-compose.yml`（请按实际部署路径与域名调整）：

```yaml
services:
  caddy:
    image: caddy:2
    ports:
      - "443:443"
      - "80:80"
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
    depends_on:
      - media-static

  media-static:
    # 生产环境请进一步固定到已审核的版本与镜像 digest
    image: nginxinc/nginx-unprivileged:stable-alpine
    read_only: true
    security_opt:
      - no-new-privileges:true
    cap_drop:
      - ALL
    # 只读挂载插件 cache 目录，容器内以 root 之外的用户运行
    volumes:
      - /path/to/astrbot/data/plugin_data/astrbot_plugin_parser/cache:/usr/share/nginx/html:ro
    # 仅内网可达；由主 Caddy 转发，不直接暴露公网
```

主 Caddy 的 `Caddyfile` 示意：

```caddy
media.example.com {
    @napcat remote_ip 203.0.113.10/32
    handle @napcat {
        reverse_proxy media-static:8080
    }
    respond "Forbidden" 403
}
```

对应插件配置：

```json
{
    "media_send_mode": "http",
    "media_http_base_url": "https://media.example.com"
}
```

> 若静态容器的根目录直接指向 cache 目录，`media_http_base_url` 无需带子路径；
> 插件会自动在 base URL 后拼接 cache 内的相对路径。若静态服务挂在子路径下
> （如 `/astrbot`），则按实际子路径填写 base URL。

### 使用说明与注意事项

- 切换为 `http` 前，请确认 NapCat 所在主机可以访问上述 URL（含端口/防火墙），
  否则远端无法拉取媒体。
- TLS、IP 访问控制（ACL）与静态服务本身由部署方负责，插件不参与；
  建议将静态容器限制在内网，并由主 Caddy 统一管理证书与访问策略。
- 部分缓存文件名可能由来源 ID 等信息派生，不能依赖“文件名不可猜测”作为安全
  边界；应由主 Caddy 配置 IP ACL，并可在 base URL 中增加随机路径前缀。
- 仍应按 `media_http_ttl` 定期清理过期文件（插件自带的清理任务在 http 模式下
  即按 TTL 清理）。
- 当 HTTP 配置缺失、非法或映射失败时，插件**不会**退回 `file://` 发送：
  轻媒体将被跳过，重媒体（视频等）会输出明确失败提示。
- 默认 `local` 模式行为完全不变，未配置新字段的旧配置亦可正常加载。

---

## 🎉 指令

|   指令   |         权限          |        说明        |
| :------: | :-------------------: |  :---------------: |
| 开启解析 |      ADMIN            |     开启当前会话的解析功能      |
| 关闭解析 |      ADMIN            |    关闭当前会话的解析功能      |
|  blogin  |      ADMIN           |   扫码获取 B 站凭证 |

---

## 🧠 插件工作流程

当插件运行后，每一条消息的处理流程如下：

1. **消息接收**  
   监听所有消息事件，获取消息链与原始文本内容  
   - 支持普通文本、链接、卡片（Json 组件）

2. **基础过滤**  
   - 跳过已被禁用的会话  
   - 跳过空消息  
   - 若消息首段为 `@` 且目标不是本 Bot，则不解析

3. **链接提取与匹配**  
   - 若为卡片消息，先从 Json 中提取 URL  
   - 使用「关键词 + 正则」双重匹配，定位对应解析器  
   - 未匹配到解析规则则直接退出

4. **仲裁判定（Emoji Like Arbiter）**  
   - 仅在 `aiocqhttp` 平台生效  
   - 通过固定表情进行 Bot 间仲裁  
   - 未胜出的 Bot 自动放弃解析

5. **防抖判定（Link Debouncer）**  
   - 对同一会话内的相同链接进行时间窗口限制  
   - 命中防抖规则则跳过解析，避免短时间重复处理

6. **内容解析**  
   - 调用对应平台解析器获取媒体信息  
   - 生成统一的 `ParseResult` 数据结构

7. **媒体下载与消息构建**  
   - 下载视频 / 图片 / 音频 / 文件  
   - 根据配置决定音频发送方式  
   - 可按配置提示下载失败项

8. **卡片渲染（可选）**  
   - 在非简洁模式或无直传媒体时生成媒体卡片  
   - 使用 PIL 渲染并缓存图片

9. **消息合并与发送**  
    - 当消息段数量超过阈值时自动合并为转发消息  
    - 最终将结果发送到对应会话

---

## 🧩 扩展

插件支持自定义解析器，通过继承 `BaseParser` 类并实现 `platform`, `handle` 即可。

示例解析器请看 [示例解析器](https://github.com/Zhalslar/astrbot_plugin_parser/blob/main/core/parsers/example.py)

---

## 🎉 致谢

本项目核心代码来自[nonebot-plugin-parser](https://github.com/fllesser/nonebot-plugin-parser)，请前往原仓库给作者点个Star!
