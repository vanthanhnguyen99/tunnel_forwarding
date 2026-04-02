# MVP project: Web quản trị SSH tunneling kiểu MobaXterm

## 1. Mô hình tunnel

Project này dùng mô hình **SSH Local Forward** kiểu MobaXterm.

Luồng kết nối:

```text
Local client
    |
    v
Local listen port
    |
    v
SSH connection to SSH server
    |
    v
Destination host:port reachable from the SSH server side
```

Tức là luôn có **SSH server đứng giữa**. Destination được reach từ phía remote qua SSH tunnel, còn control plane trên host chỉ quản lý config, lifecycle và monitoring.

---

## 2. Mục tiêu MVP

Xây dựng một hệ thống gồm:

* **Web admin** để quản trị các SSH tunnel endpoint.
* **Control plane trên host** để quản lý config, auth, metrics và lifecycle Docker.
* **Một container tunnel riêng cho mỗi endpoint**.
* Mỗi endpoint giữ **một local listener bên trong container** và forward qua SSH server tới destination.
* **Realtime monitoring** để theo dõi:
  * endpoint nào đang running,
  * bao nhiêu client đang connect vào từng endpoint,
  * tổng traffic in/out,
  * chart active connections và bandwidth theo thời gian.

Mục tiêu UX:

* tạo / sửa / xoá tunnel nhanh,
* start / stop từng endpoint,
* nhìn thấy container listen address forward đi đâu,
* biết đang đi qua SSH server nào,
* xem session đang hoạt động,
* xem chart realtime.

---

## 3. Phạm vi MVP

### Trong phạm vi

* Admin login vào web quản trị.
* CRUD endpoint.
* Mỗi endpoint là một **SSH Local Forward**.
* Mỗi endpoint có:
  * listen host bind bên trong container,
  * listen port bên trong container,
  * remote destination host,
  * remote destination port,
  * SSH server host,
  * SSH server port,
  * SSH username,
  * SSH private key path hoặc dùng `ssh-agent` / default key,
  * allowed client CIDR,
  * max clients,
  * idle timeout,
  * tags / description,
  * enable / disable.
* Nhiều client có thể cùng connect vào một endpoint.
* Mỗi client tạo **session riêng**.
* Mỗi client được tách biệt bằng **một SSH session riêng** tới SSH server.
* Nhiều endpoint có thể dùng cùng `listen_port` vì mỗi endpoint có container IP riêng.
* UI hiển thị:
  * danh sách endpoint,
  * Docker NAT IP / container listen address,
  * SSH server của endpoint,
  * active sessions,
  * total traffic,
  * chart realtime,
  * endpoint detail.

### Ngoài phạm vi MVP

* Reverse tunnel / remote forward.
* Dynamic SOCKS proxy.
* Password auth có prompt tương tác.
* SSH multiplexing / ControlMaster.
* HA / clustering.
* Multi-node workers.
* RBAC nhiều vai trò.
* TLS mutual auth phức tạp.

---

## 4. Use case chính

### Use case 1: Admin tạo tunnel mới

Admin bấm **New Tunnel** và nhập:

* Name
* Type: `SSH Local Forward`
* Listen host
* Listen port
* Destination host
* Destination port
* SSH server host
* SSH server port
* SSH username
* Private key path (optional)
* Known hosts path (optional)
* Extra SSH options (optional)
* Allowed client CIDR
* Max clients
* Idle timeout
* Description
* Tags
* Enabled/Disabled

Sau khi lưu:

* config được ghi vào SQLite,
* hệ thống tự gán một Docker NAT IP trong dải `172.20.0.0/16`,
* hệ thống tự sinh `endpoint.json` và `docker-compose.yml` tương ứng cho tunnel đó,
* nếu `enabled=true` thì app build shared runner image nếu chưa có, rồi chạy `docker compose up -d --no-build --force-recreate`,
* tunnel SSH bind listen port **bên trong container**,
* port **không publish ra host**; truy cập qua Docker NAT IP `172.20.x.x:listen_port`,
* endpoint xuất hiện ngay trên dashboard.

### Use case 2: Client connect vào tunnel

Ví dụ endpoint `ixia_tunnel_us`:

* container listen: `172.20.0.2:30001`
* container bind: `0.0.0.0:30001`
* SSH server: `10.46.4.66:22` user `thanh2n`
* destination: `10.255.205.8:1080`

Local client connect vào:

* `172.20.0.2:30001`

Runtime sẽ:

1. accept client connection,
2. local connection đó được listener bên trong container nhận,
3. OpenSSH mở channel qua SSH server tới `10.255.205.8:1080`,
4. worker trong container ghi `runtime.json` để control plane đọc session/counter,
5. đẩy event realtime lên dashboard.

### Use case 3: Admin theo dõi realtime

Dashboard hiển thị:

* endpoint nào đang running,
* endpoint nào stopped / disabled,
* endpoint nào đang đi qua SSH server nào,
* container NAT IP của từng endpoint,
* bao nhiêu session đang mở,
* top endpoint theo traffic,
* chart active connections theo thời gian,
* chart bytes in/out theo thời gian,
* bảng session đang chạy.

---

## 5. Kiến trúc

### 5.1 Control plane

* Web UI
* REST API
* Auth admin
* Config store SQLite
* SSE realtime event stream
* Docker config generator
* Docker lifecycle manager

### 5.2 Data plane

* 1 container riêng cho mỗi endpoint
* Local listener bên trong container
* Session tracker trong worker
* Per-client `ssh -W` subprocess
* Runtime state mirror (`runtime.json`)

### 5.3 Topology logic

```text
[Admin Browser]
      |
      v
[Web UI + REST API]
      |
      v
[SQLite Config Store] <----> [SSE Realtime Events]
      |
      v
[DockerTunnelManager]
   |- endpoint container A (172.20.0.2)
   |- endpoint container B (172.20.0.3)
   |- docker compose lifecycle
   |- runtime state reader

Client flow:

[Local App / Host] -> [172.20.x.x:listen_port] -> [SSH Server] -> [Destination Host:Port]
```

---

## 6. Runtime model

### 6.1 Endpoint level

Mỗi endpoint có:

* 1 Docker container riêng
* 1 Docker NAT IP cố định
* 1 local listening socket bên trong container
* config destination
* config SSH server
* nhiều active sessions

### 6.2 Session level

Mỗi client session có:

* 1 client socket
* 1 SSH subprocess `ssh -W` riêng
* byte counter up/down riêng
* start time
* close reason

### 6.3 Vì sao dùng `ssh -W`

Để đảm bảo isolation rõ ràng giữa nhiều client cùng vào một endpoint, runtime dùng:

* worker trong container tự accept local client connection
* với mỗi client, spawn:
  * `ssh -W destination_host:destination_port user@ssh_host`

Lợi ích:

* đúng topology đi qua SSH server,
* **1 client = 1 SSH transport riêng**,
* tránh shared transport giữa nhiều client,
* dễ disconnect từng session từ UI,
* metrics/session tracking chính xác hơn.

Trade-off:

* mỗi client tạo 1 SSH process riêng,
* chi phí handshake SSH cao hơn kiểu shared tunnel.

---

## 7. Thiết kế dữ liệu

### 7.1 Bảng endpoints

```text
id
name
tunnel_type
listen_host
listen_port
destination_host
destination_port
ssh_host
ssh_port
ssh_username
ssh_private_key_path
ssh_known_hosts_path
ssh_options
description
allowed_client_cidr
enabled
max_clients
idle_timeout
tags
status_message
created_at
updated_at
docker_nat_ip
docker_network_name
docker_service_name
docker_container_name
docker_compose_path
docker_endpoint_config_path
```

### 7.2 Bảng sessions

```text
id
endpoint_id
client_ip
client_port
upstream_ip
upstream_port
status
bytes_up
bytes_down
connected_at
closed_at
close_reason
```

`upstream_ip/upstream_port` ở đây là destination cuối cùng, không phải SSH server.

### 7.3 Bảng metrics_timeseries

```text
ts
endpoint_id
active_connections
bytes_up_per_sec
bytes_down_per_sec
```

---

## 8. API MVP

### Auth

* `POST /api/login`
* `POST /api/logout`
* `GET /api/me`

### Endpoints

* `GET /api/endpoints`
* `POST /api/endpoints`
* `GET /api/endpoints/:id`
* `PUT /api/endpoints/:id`
* `DELETE /api/endpoints/:id`
* `POST /api/endpoints/:id/start`
* `POST /api/endpoints/:id/stop`

### Sessions

* `GET /api/sessions`
* `GET /api/endpoints/:id/sessions`
* `POST /api/sessions/:id/disconnect`

### Metrics

* `GET /api/metrics/overview`
* `GET /api/metrics/timeseries`
* `GET /api/endpoints/:id/metrics`

### Realtime events

SSE events:

* `endpoint.created`
* `endpoint.updated`
* `endpoint.started`
* `endpoint.stopped`
* `endpoint.deleted`
* `session.opened`
* `session.closed`
* `metrics.tick`

---

## 9. UI kỳ vọng

### Dashboard

* KPI cards:
  * Total endpoints
  * Active endpoints
  * Active sessions
  * Total traffic
* Chart:
  * active connections
  * traffic in/out
  * top endpoints
* Endpoint table:
  * Name
  * Type
  * Container Listen
  * SSH server
  * Forward to
  * Container IP
  * Status
  * Clients
  * Traffic
  * Actions
* Active sessions table
* Endpoint detail panel

### Tunnel form

Field chính:

* Tunnel Name
* Listen Host (bind trong container)
* Listen Port (`1..65535`)
* Destination Host
* Destination Port (`1..65535`)
* SSH Server Host
* SSH Port
* SSH Username
* Private Key Path
* Known Hosts Path
* Extra SSH Options
* Allowed Client CIDR
* Max Clients
* Idle Timeout
* Description
* Tags
* Enable immediately

---

## 10. Current implementation trong repo

Repo hiện tại implement theo hướng:

* **Backend/UI:** Python stdlib HTTP server + HTML/CSS/JS
* **DB:** SQLite
* **Realtime:** SSE
* **Runtime:** Docker per-endpoint worker container
* **SSH transport:** OpenSSH client binary `ssh`

Điểm quan trọng:

* control plane trên host không mở direct TCP connection tới destination,
* host app không bind listen port cho tunnel,
* tunnel chỉ listen bên trong container và được truy cập qua Docker NAT IP,
* destination chỉ được reach từ phía SSH server,
* mỗi client session dùng `ssh -W destination_host:destination_port ...`,
* runner image được dùng chung giữa các endpoint và chỉ build khi image chưa tồn tại.

---

## 11. Giới hạn hiện tại của implementation

Đây là MVP đúng topology, chưa phải production-grade SSH gateway.

Hiện tại:

* hỗ trợ **key-based auth / ssh-agent / default key**,
* chưa có password prompt tương tác kiểu desktop app,
* mỗi client session là một SSH process riêng,
* preflight SSH được chạy khi start endpoint,
* process web admin cần quyền truy cập Docker daemon,
* lần build runner image đầu tiên có thể cần `docker login` hoặc `APP_DOCKER_RUNNER_IMAGE` trỏ tới image đã prebuild.

---

## 12. Chạy local

### File chính

* `tunnel_admin/`: app backend + UI + control plane + worker runtime
* `data/`: SQLite DB + secret
* `data/docker/`: per-endpoint Docker artifacts + runtime state
* `runtime/`: PID + logs
* `Dockerfile.tunnel-runner`
* `start.sh`
* `stop.sh`

### Env

Copy env mẫu:

```bash
cp .env.example .env
```

Biến quan trọng:

* `APP_HOST`
* `APP_PORT`
* `APP_DATA_DIR`
* `APP_RUNTIME_DIR`
* `ADMIN_DEFAULT_USER`
* `ADMIN_DEFAULT_PASS`
* `APP_DOCKER_NETWORK_NAME`
* `APP_DOCKER_NETWORK_SUBNET`
* `APP_DOCKER_RUNNER_IMAGE`

### Yêu cầu môi trường

* `python3`
* Docker daemon + Docker CLI
* process chạy app phải truy cập được Docker socket

### Start

```bash
./start.sh
```

### Stop

```bash
./stop.sh
```

### Ghi chú deploy

* `start.sh` chỉ start web admin process, không force restart process cũ nếu PID file vẫn hợp lệ.
* Tunnel container được start/stop từ UI hoặc API.
* Nếu lần start đầu tiên bị Docker Hub rate limit, dùng:

```bash
docker login
```

hoặc prebuild image rồi set:

```bash
export APP_DOCKER_RUNNER_IMAGE=your-registry/tunnel-forwarding-runner:tag
```

### Web URL

```text
http://127.0.0.1:2020
```


---

## 13. Hướng nâng cấp sau MVP

Nếu muốn tiến gần hơn tới behavior của MobaXterm production:

* thêm password auth an toàn,
* thêm SSH ControlMaster / multiplexing,
* thêm reverse tunnel / dynamic SOCKS,
* thêm audit log UI,
* export Prometheus,
* chuyển data plane sang daemon tối ưu hơn.
