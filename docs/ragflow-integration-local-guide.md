# RAGFlow 集成手册(本机:复用智控台 mysql/redis 版)

> 面向本机环境(`E:\my-server` 的 Docker 部署):智控台容器(mysql/redis)运行在 docker 网络
> `xiaozhi-server_default` 上,**未开放公网端口**;Agent(xiaozhi-esp32-server)走「智控台 API 下发配置」模式。
>
> 结论前置:**RAGFlow 尚未部署**。按本手册完成后,可实现:智控台创建知识库 → 上传/解析文档 →
> Agent(function-call)在对话中自主调用知识库检索并回答。

---

## 0. 端口方案:按你的 FRP 模式理解(重要,先读)

你的网络是 **FRP 统一做公网入口**:本机服务端口由 Docker 发布到本机/局域网,再由 frpc 选择性转发到
公网 VPS(frps),设备通过 `47.108.216.163:<端口>` 访问。**一个口公网可达与否 = 是否在 frpc 里配了转发**,
与 Docker 是否绑 `0.0.0.0` 无关;设备"联网即用"走的就是这条链路(如 `ws://…:8000`)。

把口分三类:

| 类型 | 哪些口 | 是否走 FRP | 说明 |
|---|---|---|---|
| 设备直连的口 | xiaozhi ws/http(8000/8003)、智控台(8002)等 | ✅ 已走 FRP | 设备/你访问入口,已生效 |
| 后端内部互调的口 | mysql(3306)、redis(6379)、RAGFlow 检索口 | ❌ **不要走 FRP** | 只有 xiaozhi-server / 智控台后端在 docker 内网里调它;设备永不直连 |
| 你远程要开的管理页 | RAGFlow 网页(8008) | ⭕ 按需加 FRP | 想在外网浏览器直接开 ragflow 管理页时才加(见 2.6) |

> ⚠️ **为什么数据库绝不走 FRP:** 设备链路是 `设备 →(公网 ws)→ xiaozhi-server →(内网)→ RAGFlow/mysql`。
> 把 3306/6379 放到公网**不会让任何设备多一项能力**,只会把带默认口令(root/123456、redis 无密码)的库
> 暴露给全网。别加。

**本手册的连库做法(与官方文档的差异):** 官方文档假设 ragflow 用 `host.docker.internal` 访问宿主机
mysql/redis,因此要把智控台 db/redis 的 `expose` 改成 `ports` 发布出来。本机**不需要**这么做——
让 RAGFlow 需要连库的容器**挂到智控台已有的 docker 网络 `xiaozhi-server_default`**,直接用**容器名**
连 mysql/redis。docker 内网连通性不受 FRP 影响,也不依赖任何宿主机端口。

| 项 | 官方文档 | 本机(本手册) |
|---|---|---|
| RAGFlow 连 mysql/redis | `host.docker.internal:3306/6379`(需先改 compose 发布端口) | 容器名 `xiaozhi-esp32-server-db` / `xiaozhi-esp32-server-redis`(挂同一 docker 网络,无需改智控台 compose) |
| 智控台 db/redis 端口 | 需 `expose` 改 `ports` | **不用改**,保持内网 |
| RAGFlow UI | `http://IP:8008` | 本机/局域网 `http://<本机IP>:8008`;要外网管理再加 FRP(2.6) |
| 智控台模型配置里的「服务地址」 | `http://局域网IP:8008` | `http://ragflow-cpu`(docker 网络别名,Java 端与 Python Agent 都能解析访问) |

> 依赖事实(已核实):mysql 容器的 root/123456、库 `xiaozhi_esp32_server`;redis 无密码;
> 四个 xiaozhi 容器同属网络 `xiaozhi-server_default`。

---

## 第一部分:在智控台 mysql 里建 RAGFlow 的库和账号

不需要开放端口,直接在容器里执行。

在项目目录(`E:\my-server\main\xiaozhi-server`)下用 git-bash 执行:

```bash
docker exec -i xiaozhi-esp32-server-db mysql -uroot -p123456 <<'SQL'
CREATE DATABASE IF NOT EXISTS rag_flow CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER IF NOT EXISTS 'rag_flow'@'%' IDENTIFIED BY 'infini_rag_flow';
GRANT ALL PRIVILEGES ON rag_flow.* TO 'rag_flow'@'%';
FLUSH PRIVILEGES;
SQL
```

验证(应能列出 rag_flow 库):

```bash
docker exec xiaozhi-esp32-server-db mysql -uroot -p123456 -e "SHOW DATABASES LIKE 'rag_flow';"
```

---

## 第二部分:部署 RAGFlow 服务

### 2.1 拉取项目并切到已验证版本

官方适配代码/文档以 **v0.22.0** 为准,先按这个版本装(国内网络慢可用 ghfast 前缀)。

```bash
cd /e/my-server
git clone https://github.com/infiniflow/ragflow.git
cd ragflow
git checkout v0.22.0
```

> 若官方 tag 拉不动,可用镜像:`git clone https://ghfast.top/https://github.com/infiniflow/ragflow.git`

### 2.2 解除对自带 mysql/redis 的依赖(核心)

进 `ragflow/docker` 目录,下面所有操作都在这里:

```bash
cd /e/my-server/ragflow/docker
```

**先列出实际结构,和文档对上号(文档与你的版本结构可能略不同):**

```bash
ls -la docker-compose*.yml && grep -nE "^  [a-z_]+:|profiles:|image:|container_name:" docker-compose.yml docker-compose-base.yml | head -80
```

> ⚠️ **检查点:** 把上面输出和 `E:\my-server\docs\ragflow-integration.md` 第一部分的截图/描述比对。
> 若服务名不是 `ragflow-cpu`/`ragflow-gpu`,或 base 文件结构对不上,**先停下来把输出发给我**,不要硬改。

然后按官方文档做两处删除(以你看到的实际内容为准):

1. **`docker-compose.yml`**:删掉 `ragflow-cpu`(和你实际启动的那个 profile 服务)里对 `mysql` 的
   `depends_on`(原为 `condition: service_healthy`)。GPU 服务同理。
2. **`docker-compose-base.yml`**:删除 `mysql:` 和 `redis:` 两个 service 整块,**其余(如 minio、es/infinity 等)全部保留**。

### 2.3 把 ragflow 服务挂到智控台网络(★ 必做,漏了 = "网页能开但注册不了")

> 这一步漏掉的典型症状:容器日志报 `Failed to resolve 'xiaozhi-esp32-server-db'`,**网页能打开,
> 但注册/登录失败**(账号要写智控台的 MySQL,连不上库)。实测根因就是这步没落到 compose 上。

实际结构:ragflow 的服务挂在它自建的网络(键名 `ragflow`,实际网络 `docker_ragflow`);
智控台网络是 `xiaozhi-server_default`(bridge)。要让 ragflow **同时**挂在这两个网络上:

1) 在 `ragflow/docker/docker-compose.yml` 里增加外部网络声明(文件末尾即可;ragflow 自带的
   `ragflow` 网络在 docker-compose-base.yml 里声明,别动它):

```yaml
networks:
  xiaozhi:
    external: true
    name: xiaozhi-server_default
```

2) 在你启动的 profile 服务(本机是 `ragflow-cpu`)下,把它加入两个网络:

```yaml
    networks:
      - ragflow
      - xiaozhi
```

> 目的:让 ragflow 与智控台的 mysql/redis **同处一个 docker 网络**,直接用容器名
> (`xiaozhi-esp32-server-db` / `xiaozhi-esp32-server-redis`)访问,不需要向宿主机开放任何端口。

### 2.4 修改 `.env`,连到智控台的库

编辑 `ragflow/docker/.env`,找到并逐个改:

```dotenv
SVR_WEB_HTTP_PORT=8008          # ragflow 网页端口(本机浏览器用)
SVR_WEB_HTTPS_PORT=8009

# MySQL —— 指向智控台的容器名(同一 docker 网络),不是 host.docker.internal
MYSQL_HOST=xiaozhi-esp32-server-db
MYSQL_PORT=3306
MYSQL_USER=rag_flow            # ← 如果没有这一项,必须新增!漏了会启动失败
MYSQL_PASSWORD=infini_rag_flow
MYSQL_DBNAME=rag_flow

# Redis —— 指向智控台的 redis(无密码)
REDIS_HOST=xiaozhi-esp32-server-redis
REDIS_PORT=6379
REDIS_PASSWORD=
```

再把 `docker/service_conf.yaml.template` 里 redis 默认密码占位改成空(否则连不上智控台的 redis):

改前:
```yaml
  password: '${REDIS_PASSWORD:-infini_rag_flow}'
```
改后:
```yaml
  password: '${REDIS_PASSWORD:-}'
```

### 2.5 确认智控台能访问 ragflow 的地址(填模型配置要用)

启动后确认从智控台/Agent 容器能解析到 ragflow(挂网成功后是同一个 172.18.x.x 网段):

```bash
docker exec xiaozhi-esp32-server-web sh -c 'getent hosts ragflow-cpu'
```

能解析出 IP 即可。**`ragflow-cpu`(compose 服务名别名)或容器名 `docker-ragflow-cpu-1`
就是智控台模型配置里要填的服务地址**(见第四部分)。不要填公网地址。
(可选:想用更短的名字,可在该服务上加 `container_name: ragflow`,则地址填 `http://ragflow`;非必须。)

### 2.6 RAGFlow 网页口怎么对外(按你的 FRP 模式)

`SVR_WEB_HTTP_PORT=8008` 会被 Docker 发布到 `0.0.0.0:8008`(本机/局域网可访问)。对外是否可达由 **frpc** 决定:

- **只在服务器本机/局域网开 ragflow 页面**(比如你 RDP 在这台机器上操作):什么都不用加,浏览器访问
  `http://127.0.0.1:8008` 或 `http://<本机局域网IP>:8008` 即可。
- **想在外面浏览器远程管理 ragflow**:给它单独加一条 FRP 转发(不要动数据库端口)。在你运行 frpc 的那台
  入口配置里加:

```ini
# frpc.ini / frpc.toml(示例,tcp 方式)
[ragflow-web]
type = tcp
local_ip = 127.0.0.1
local_port = 8008
remote_port = 8008
```

  之后在外网浏览器访问 `http://47.108.216.163:8008`。
- **给 ragflow 网页加 FRP 之后**,智控台模型配置里的「服务地址」仍然填 `http://ragflow-cpu`(内网直达、快且稳),
  不要填公网地址——否则 Java 端和 Python Agent 每次检索都要绕公网一圈,还可能被 FRP/hairpin 挡掉。

> 🔒 **再次强调:不要给 mysql(3306)/redis(6379)配置任何 FRP 转发。** 设备与 ragflow 都走内网,
> 数据库没有公网需求;若 frpc 里存在把 3306/6379 映射出去的规则,请删掉。

### 2.7 启动 ragflow(记得带上文档引擎 profile)

```bash
docker compose --profile cpu --profile elasticsearch -f docker-compose.yml up -d
```

> `.env` 里 `DOC_ENGINE=elasticsearch`(默认),但 es01 服务带 `profiles: [elasticsearch]`,
> **只 `--profile cpu` 不会启动它** → 日志 `Failed to resolve 'es01'` 刷屏、后端初始化不完全。
> 所以 **`cpu` 和 `elasticsearch` 两个 profile 都要带**(首次会拉取 `elasticsearch:8.11.3`,约 1.3GB)。
> 若以后改用其他文档引擎(infinity/opensearch),替换为对应 profile 即可。启动后:

```bash
docker compose ps
docker logs -f <ragflow-server容器名>        # 观察无报错、跑完初始化(migrations)即成功
```

> ⚠️ **检查点:** 这一步最容易暴露前面改错的点。常见报错:
> - 连不上 mysql:`Access denied` → 检查 MYSQL_USER/PASSWORD、是否已执行第一部分建号;
> - 连不上 mysql:`Unknown database 'rag_flow'` → 检查 `MYSQL_DBNAME`;
> - 连不上 redis → 确认 `service_conf.yaml.template` 密码已置空、`REDIS_HOST` 是容器名;
> - 网络不通 → 确认 2.3 外部网络名是否为 `xiaozhi-server_default`(`docker network ls` 复核)。
>
> 卡住就把 `docker logs -n 100` 输出发我。

---

## 第三部分:RAGFlow 网页端初始化

1. 浏览器(在服务器本机)打开 `http://127.0.0.1:8008`,`Sign Up` 注册账号、`Sign In` 登录。
   不想开放注册则把 `.env` 里 `REGISTER_ENABLED=0` 后重启。
2. 右上角头像 → 设置页 → 左侧 **模型供应商**:
   - `TEXT EMBEDDING`:**必须**配一个(解析文档、建索引要用)。选你已有 API 的供应商,填 Key。
   - `LLM`:**可选**。知识库问答/解析不需要,Agent 的最终回答用的是智控台配的 LLM。省钱可跳过。
   - 配完刷新,在「设置默认模型」里把 Embedding(和 LLM,若配了)设为默认。
3. 头像 → **API** → **API Key** → `Create new key`,复制生成的 Key(稍后填进智控台)。

> 建议:Embedding 选你智控台已经在用的同一供应商(如阿里百炼/dashscope、智谱),少申请一个账号。

---

## 第四部分:智控台接入(让 Agent 用上知识库)

> 智控台(web)在宿主机 `http://127.0.0.1:8002`,用超级管理员登录。
> 以下为官方文档第二部分操作,服务地址改成本机 docker 网络地址。

1. **开启知识库功能**:顶部「参数字典」→「系统功能配置」→ 勾选「知识库」→ 保存。刷新后顶部出现「知识库」。
2. **配置 RAGFlow 模型**:顶部「模型配置」→ 左侧「知识库」→ 找到 `RAG_RAGFlow`,点「编辑」:
   - 服务地址:`http://ragflow-cpu`(或 `http://docker-ragflow-cpu-1`)
   - API 密钥:第三部分复制的 Key
   - 保存。
   > 若列表里没有 `RAG_RAGFlow`,点「新增」按同名字段创建。
3. **创建知识库并喂文档**:「知识库」→「新增」→ 起个有意义的名字和描述(描述会被写进工具的调用说明,
   影响大模型何时检索)→ 进入详情 →「新增」上传文档 → 点「解析」→ 解析完可点「召回测试」验证命中。
4. **绑定到智能体**:顶部「智能体」→ 找到设备/对话用的智能体 →「配置角色」→ 在「编辑功能」弹框中勾选刚建的知识库 → 保存。
   > 这一步会让智控台把 `search_from_ragflow` 这个检索工具及其参数(base_url/api_key/dataset_ids)
   > 通过配置下发到 xiaozhi Agent,对应代码
   > `main/manager-api/.../agent/service/impl/AgentPluginMappingServiceImpl.java`。
5. **联动校验(可选)**:`xiaozhi-esp32-server` 读取的是智控台下发配置,
   若此前它缓存过旧配置,重启该容器让新配置生效:

```bash
docker restart xiaozhi-esp32-server
```

---

## 第五部分:端到端验证

1. 容器内自测 RAGFlow 检索 API(证明 Python Agent 能访问该地址):
```bash
docker exec -i xiaozhi-esp32-server bash -lc \
  'curl -sk -X POST http://ragflow-cpu/api/v1/retrieval \
    -H "Authorization: Bearer <你的APIKey>" -H "Content-Type: application/json" \
    -d "{\"question\":\"测试\",\"dataset_ids\":[\"<知识库datasetId>\"]}"'
```
   datasetId 可在智控台知识库列表/详情 URL 里看到(形如 UUID)。能返回 `"chunks":[...]` 即通。
2. 用数字人/测试客户端(shell 里跑 `digital-human` 或访问 `/test/`)对 Agent 提问知识库里的内容,
   观察它是否先调 `search_from_ragflow` 再据此回答(服务日志会打印工具调用)。

---

## 常见问题(FAQ)

| 现象 | 处理 |
|---|---|
| 想在外面浏览器直接管理 ragflow 页面 | 给 ragflow 网页口 8008 加一条 FRP 转发(见 2.6);数据库口不用、也别加 |
| 为什么 mysql/redis 不开放到公网,设备还能用知识库 | 设备只连 xiaozhi-server(公网 ws),知识库检索是 xiaozhi 在后台内网完成的,数据库公网可达与否与设备无关 |
| ragflow 起不来,日志 `Access denied` | mysql 账号没建好 → 回第一部分,并确认 `.env` 的 MYSQL_USER/PASSWORD |
| ragflow 起不来,日志连 redis 报错 | `service_conf.yaml.template` redis password 未置空,或 REDIS_HOST 拼错 |
| 智控台创建/解析知识库报「连接失败」 | 模型配置 `RAG_RAGFlow` 的服务地址应为 `http://ragflow-cpu`(智控台容器能解析到),API Key 是否有权限 |
| Agent 回答不检索 | 智能体「编辑功能」未勾知识库;或检索工具未进入该智能体 function 列表(重启 xiaozhi-server 后再试) |
| Agent 检索超时(`RAG接口请求超时`) | `search_from_ragflow.py` 超时仅 5s;首次解析慢属正常,多做一轮或检查 ragflow 负载 |

## 回滚

RAGFlow 只新增容器和 `rag_flow` 库,不动智控台任何数据:

```bash
cd /e/my-server/ragflow/docker
docker compose --profile cpu down
# 数据库账号删除(可选):
docker exec -i xiaozhi-esp32-server-db mysql -uroot -p123456 -e "DROP DATABASE IF EXISTS rag_flow; DROP USER IF EXISTS 'rag_flow'@'%';"
```
