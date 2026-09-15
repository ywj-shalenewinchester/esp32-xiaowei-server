#!/usr/bin/env bash
# 小智服务端口体检 / 一键修复
#
# 背景：Docker Desktop(Windows/WSL2) 在宿主机或 Docker Desktop 重启后，
#       宿主机→容器的端口转发会【按端口各自悬空】。症状很能骗人：
#       docker ps 显示 Up、docker logs 也正常、容器内部一切好，但外部连不上。
#       TCP 能建立（被 com.docker.backend.exe 接走）却返回空响应，应用日志里
#       没有任何请求记录。2026-09-12 那次重启就让 8000 和 8002 同时废掉，
#       而同容器的 8003 一直正常。
#
# 判断逻辑：
#       宿主机不通 + 容器内通   =>  转发失效，需要 --force-recreate 重建容器
#       宿主机不通 + 容器内不通 =>  应用没起来，去看 docker logs <容器>
#
# 用法：
#   bash check_ports.sh          只体检
#   bash check_ports.sh --fix    体检并自动重建有问题的容器

set -u

# Git Bash 会把 /dev/tcp/... 当路径改写，必须关掉路径转换
export MSYS_NO_PATHCONV=1

cd "$(dirname "$0")" || exit 1
COMPOSE_FILE="docker-compose_all.yml"
FIX=0
[ "${1:-}" = "--fix" ] && FIX=1

# 端口 容器名 探测方式 探测路径 所属 compose 服务(空=不在本文件的 compose 里) 说明
PORTS=(    8000                     8003              8002                    8004              )
CONTAINERS=(xiaozhi-esp32-server    xiaozhi-esp32-server xiaozhi-esp32-server-web mcp-endpoint-server)
MODES=(    ws                       http              http                    http              )
PATHS=(    /xiaozhi/v1/             /mcp/vision/explain /                   /                 )
SERVICES=( xiaozhi-esp32-server     xiaozhi-esp32-server xiaozhi-esp32-server-web ""              )
DESCS=(    "设备接入(websocket)"    "HTTP(视觉分析)"  "智控台"                "MCP接入点"        )

# 从宿主机探活，返回 http_code；000 = 没拿到任何 HTTP 响应(转发断了)
host_probe() {
    local port=$1 mode=$2 path=$3
    if [ "$mode" = "ws" ]; then
        curl -s -o /dev/null -m 8 -w "%{http_code}" \
            -H "Connection: Upgrade" -H "Upgrade: websocket" \
            -H "Sec-WebSocket-Version: 13" \
            -H "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==" \
            "http://127.0.0.1:${port}${path}" 2>/dev/null
    else
        curl -s -o /dev/null -m 8 -w "%{http_code}" "http://127.0.0.1:${port}${path}" 2>/dev/null
    fi
}

# 从容器内部探活(绕开转发)，0 = 能连上
inner_probe() {
    docker exec "$1" bash -c "exec 3<>/dev/tcp/127.0.0.1/$2" >/dev/null 2>&1
}

# 重建容器让它重新注册端口映射。注意 docker restart 没用，必须 --force-recreate
recreate() {
    docker compose -f "$COMPOSE_FILE" up -d --force-recreate --no-deps "$1" >/dev/null 2>&1
}

echo "=== 小智服务端口体检（从宿主机探测）==="
broken_svcs=()      # 转发失效、需要重建的 compose 服务
broken_ports=()     # 转发失效的端口
down_ports=()       # 应用/容器本身没起来的端口，重建容器治不了

for i in "${!PORTS[@]}"; do
    port=${PORTS[$i]}
    container=${CONTAINERS[$i]}
    mode=${MODES[$i]}
    path=${PATHS[$i]}
    svc=${SERVICES[$i]}
    desc=${DESCS[$i]}

    code=$(host_probe "$port" "$mode" "$path")
    if [ "$code" != "000" ]; then
        printf "  %-5s %-22s %-16s OK (%s)\n" "$port" "$desc" "$container" "$code"
        continue
    fi

    printf "  %-5s %-22s %-16s 不通   " "$port" "$desc" "$container"
    if ! docker inspect "$container" >/dev/null 2>&1; then
        echo "（容器不存在）"
        down_ports+=("$port")
        continue
    fi

    if inner_probe "$container" "$port"; then
        echo "→ 容器内正常，宿主机转发失效"
        [ -n "$svc" ] && broken_svcs+=("$svc")
        broken_ports+=("$port")
    else
        echo "→ 容器内也不通，应用没起来，看 docker logs $container"
        down_ports+=("$port")
    fi
done

if [ ${#broken_ports[@]} -eq 0 ] && [ ${#down_ports[@]} -eq 0 ]; then
    echo "全部正常。"
    exit 0
fi

echo
if [ ${#down_ports[@]} -gt 0 ]; then
    echo "以下端口是应用/容器本身没起来，重建容器没用，先看 docker logs："
    printf '    %s\n' "${down_ports[@]}"
    echo
fi
if [ ${#broken_svcs[@]} -eq 0 ]; then
    [ ${#broken_ports[@]} -gt 0 ] && echo "有端口转发失效，但涉及的服务不在 $COMPOSE_FILE 里，请手动处理其部署方式。"
    exit 1
fi

# 同一容器可能有多个端口挂掉，去重
mapfile -t uniq_svcs < <(printf '%s\n' "${broken_svcs[@]}" | sort -u)

if [ "$FIX" -eq 0 ]; then
    echo "需要重建这些服务来重新注册端口映射："
    printf '    %s\n' "${uniq_svcs[@]}"
    echo "执行：bash check_ports.sh --fix"
    exit 1
fi

echo "=== 重建服务以重新注册端口映射 ==="
for svc in "${uniq_svcs[@]}"; do
    echo "  重建 $svc ..."
    recreate "$svc"
done

# server 要加载 ASR 模型，等它起来
echo "  等待服务就绪（最多 120 秒）..."
ok=0
for _ in $(seq 1 24); do
    sleep 5
    ok=1
    for i in "${!PORTS[@]}"; do
        port=${PORTS[$i]}
        case " ${broken_ports[*]} " in
            *" $port "*) [ "$(host_probe "$port" "${MODES[$i]}" "${PATHS[$i]}")" = "000" ] && ok=0 ;;
        esac
    done
    [ "$ok" -eq 1 ] && break
done

echo
echo "=== 复检 ==="
fail=0
for i in "${!PORTS[@]}"; do
    code=$(host_probe "${PORTS[$i]}" "${MODES[$i]}" "${PATHS[$i]}")
    if [ "$code" = "000" ]; then
        printf "  %-5s %-22s 仍不通\n" "${PORTS[$i]}" "${DESCS[$i]}"
        fail=1
    else
        printf "  %-5s %-22s OK (%s)\n" "${PORTS[$i]}" "${DESCS[$i]}" "$code"
    fi
done

if [ "$fail" -eq 1 ]; then
    echo
    echo "仍有端口不通。逐个重建没解决的话，重启 Docker Desktop 更彻底（会中断所有容器）。"
    exit 1
fi
echo "已全部恢复。"
