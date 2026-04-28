# astrbot_plugin_qqip

AstrBot 群聊 IP 归属地记录插件。

说明：
- 本插件只使用 AstrBot 接收到的原始消息事件数据，不会主动扫描用户设备。
- 如果平台适配器没有提供 IP 字段，插件不会生成记录。
- 建议仅在你明确告知群成员的场景下使用。

## 功能

- 监听群消息，尝试提取原始事件中的 IP 信息。
- 将 IP 解析为归属地（国家/省/市）。
- 管理员可通过指定 QQ 号查询该账号已记录到的 IP 与归属地。
- 支持生成同意访问链接：对方先在页面点击同意，再记录匿名访客ID与地区。

## 指令

- /qqip QQ号
- /qqip调试 QQ号
- /qqip链接 标题(可选)
- /qqip记录 链接ID

清空当前群记录：

- /qqip清空
- /清空qqip


## 依赖

请确保安装 requirements.txt 中依赖。

## 兼容性

- 主要面向 QQ 相关适配器（aiocqhttp / qq_official）。
- 其他平台如果原始事件中存在 IP 字段，也可正常工作。

## 排障

- 如果提示“有发言记录，但当前平台事件未提供可用 IP 字段”，说明插件已经识别到该 QQ 发言，但适配器没有上报 IP。
- 这类情况下无法定位到市级，需检查你使用的适配器是否支持并开启了 IP 上报。
- 你当前若使用 aiocqhttp(OneBot v11)，大多数实现默认不会提供用户 IP，这是平台侧限制而不是插件 bug。
- 可用 /qqip调试 QQ号 查看最近事件里是否存在任何网络相关字段样本。

## 同意链接模式说明

- 执行 /qqip链接 后，插件会生成一个网页链接。
- 访问者打开后会先看到说明页，只有点击“我同意并继续访问”才会记录。
- 记录内容为：匿名访客ID、地区、时间；不保存原始 IP。
- 管理员可用 /qqip记录 链接ID 查看统计，输出格式接近“谁在窥屏”样式。

## 环境变量

- QQIP_TRACKER_HOST: 内置同意页面 HTTP 服务监听地址，默认 0.0.0.0
- QQIP_TRACKER_PORT: 监听端口，默认 8787
- QQIP_PUBLIC_BASE_URL: 生成链接使用的公网前缀，例如 https://your-domain.com

如果不设置 QQIP_PUBLIC_BASE_URL，插件会生成本地地址链接，群友通常无法直接访问。

## 插件配置（WebUI）

- 本插件已注册配置文件 [_conf_schema.json](_conf_schema.json)。
- 你可以在 AstrBot 插件管理中直接可视化配置以下项目：tracker_host、tracker_port、public_base_url、show_limit、max_records_per_group。
- WebUI 配置优先于同名环境变量。
