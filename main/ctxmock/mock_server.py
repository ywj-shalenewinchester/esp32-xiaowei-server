"""上下文源 Mock 服务(与 docs/context-provider-integration.md 附录一致)"""
import http.server, socketserver, json

PORT = 8081


class Handler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        resp = {}
        if self.path == "/health":
            device_id = self.headers.get("device-id", "unknown")
            resp = {
                "code": 0, "msg": "success",
                "data": {
                    "测试设备ID": device_id,
                    "心率": "80 bpm",
                    "血压": "120/80 mmHg",
                    "状态": "良好",
                },
            }
        elif self.path == "/news/list":
            resp = {
                "code": 0, "msg": "success",
                "data": ["今日头条：AI Agent 学习日报上线",
                         "本地新闻：明日有大雨，记得带伞"],
            }
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(json.dumps({"code": 404}).encode())
            return

        body = json.dumps(resp, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        print(*a, flush=True)


socketserver.TCPServer.allow_reuse_address = True
with socketserver.TCPServer(("", PORT), Handler) as httpd:
    print(f"ctx-mock listening on :{PORT}", flush=True)
    httpd.serve_forever()
