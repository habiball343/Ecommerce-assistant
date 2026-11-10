#!/usr/bin/env python3
"""电商小二 · 本地启动器（只依赖 Python 标准库）。

    python scripts/xiaoer.py start            一键启动五个应用（SQLite + 进程内事件总线，零外部依赖）
    python scripts/xiaoer.py start --docker   标准部署模式：PostgreSQL + Redis（由 Docker 启动）
    python scripts/xiaoer.py stop             停止由本启动器启动的应用
    python scripts/xiaoer.py status           查看各应用是否在运行
    python scripts/xiaoer.py test             运行全链路自检
    python scripts/xiaoer.py reset            清空本地 SQLite 数据，恢复到初始演示数据

Windows 用户直接双击项目根目录下的 start.bat / stop.bat / test.bat 即可。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = ROOT / "logs"
RUNTIME_DIR = ROOT / ".runtime"
PID_FILE = RUNTIME_DIR / "pids.json"
IS_WINDOWS = os.name == "nt"
DEFAULT_MIRROR = "https://mirrors.aliyun.com/pypi/simple/"


@dataclass(frozen=True)
class Backend:
    name: str
    title: str
    module: str
    port: int

    @property
    def directory(self) -> Path:
        return ROOT / self.name

    @property
    def python(self) -> Path:
        venv = self.directory / ".venv"
        return venv / "Scripts" / "python.exe" if IS_WINDOWS else venv / "bin" / "python"


@dataclass(frozen=True)
class Frontend:
    name: str
    title: str
    port: int

    @property
    def directory(self) -> Path:
        return ROOT / "frontend" / self.name


# 启动顺序：电商服务 → 智能服务 → 客服服务 → 两个前端
BACKENDS = (
    Backend("ecommerce-service", "电商服务", "ecommerce_service.server", 8001),
    Backend("ai-service", "智能服务", "ai_service.main", 8002),
    Backend("customer-service", "客服服务", "customer_service.main", 8000),
)
FRONTENDS = (
    Frontend("user-frontend", "用户端", 5173),
    Frontend("admin-frontend", "管理端", 5174),
)

PG_PORT = int(os.environ.get("XIAOER_PG_PORT", "5432"))
REDIS_PORT = int(os.environ.get("XIAOER_REDIS_PORT", "6379"))
PG_BASE = f"postgresql+psycopg://customer_service:customer_service@127.0.0.1:{PG_PORT}"
# --docker 模式下注入给三个后端的环境变量（优先级高于各自的 .env）
DOCKER_ENV = {
    "ECOM_DATABASE_URL": f"{PG_BASE}/ecommerce",
    "DATABASE_URL": f"{PG_BASE}/customer_service",
    "AI_DATABASE_URL": f"{PG_BASE}/ai_customer_service",
    "REDIS_URL": f"redis://127.0.0.1:{REDIS_PORT}/0",
}


class LauncherError(RuntimeError):
    """可以直接展示给用户的错误。"""


# --------------------------------------------------------------------------- 输出与基础工具


def say(message: str = "") -> None:
    print(message, flush=True)


def step(message: str) -> None:
    say(f"\n==> {message}")


def find_executable(name: str, extra_dirs: tuple[Path, ...] = ()) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    suffixes = (".exe", ".cmd", ".bat", "") if IS_WINDOWS else ("",)
    for directory in extra_dirs:
        for suffix in suffixes:
            candidate = directory / f"{name}{suffix}"
            if candidate.is_file():
                return str(candidate)
    return None


def find_uv() -> str | None:
    home = Path.home()
    return find_executable("uv", (home / ".local" / "bin", home / ".cargo" / "bin"))


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(("127.0.0.1", port)) == 0


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 访问本机服务不走系统代理


def http_json(url: str, timeout: float = 2.0) -> dict | None:
    try:
        with _OPENER.open(url, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None


def tail(path: Path, lines: int = 30) -> str:
    try:
        content = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(content[-lines:])


def run(command: list[str] | str, cwd: Path, env: dict[str, str] | None = None) -> None:
    """运行安装类命令，输出直接显示在当前窗口。"""
    shell = isinstance(command, str)
    result = subprocess.run(command, cwd=cwd, env=env, shell=shell)  # noqa: S602 - 命令为固定字符串
    if result.returncode != 0:
        shown = command if shell else " ".join(command)
        raise LauncherError(f"命令执行失败（退出码 {result.returncode}）：{shown}\n  目录：{cwd}")


# --------------------------------------------------------------------------- 安装依赖


def ensure_env_file(directory: Path) -> None:
    env_file, example = directory / ".env", directory / ".env.example"
    if not env_file.exists() and example.exists():
        shutil.copyfile(example, env_file)
        say(f"  已根据 .env.example 创建 {env_file.relative_to(ROOT)}")


def install_backend(backend: Backend, uv: str | None, mirror: str | None) -> None:
    say(f"  [{backend.title}] 安装 Python 依赖 …")
    if uv and not mirror:
        # 严格按 uv.lock 安装；uv 会自动准备符合要求的 Python 版本（>= 3.12）
        run([uv, "sync", "--frozen", "--no-dev"], cwd=backend.directory)
        return

    # 没有 uv，或指定了国内镜像：按 pyproject.toml 的依赖列表安装
    import tomllib

    with (backend.directory / "pyproject.toml").open("rb") as handle:
        dependencies = tomllib.load(handle)["project"]["dependencies"]
    index_args = ["--index-url", mirror] if mirror else []
    if uv:
        if not backend.python.exists():
            run([uv, "venv", "--python", "3.12", ".venv"], cwd=backend.directory)
        run([uv, "pip", "install", "--python", str(backend.python), *index_args, *dependencies], cwd=backend.directory)
        return
    if sys.version_info < (3, 12):
        raise LauncherError(
            "没有找到 uv，且当前 Python 版本低于 3.12。\n"
            "  请先安装 uv： https://docs.astral.sh/uv/getting-started/installation/\n"
            '  Windows PowerShell： powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"'
        )
    if not backend.python.exists():
        run([sys.executable, "-m", "venv", ".venv"], cwd=backend.directory)
    run([str(backend.python), "-m", "pip", "install", "--quiet", *index_args, *dependencies], cwd=backend.directory)


def install_frontend(frontend: Frontend) -> None:
    if (frontend.directory / "node_modules" / "vite" / "bin" / "vite.js").exists():
        return  # 已安装过；需要重装时删除 node_modules 目录即可
    say(f"  [{frontend.title}] 安装前端依赖（npm install）…")
    # Windows 上 npm 是 npm.cmd，需要通过系统 shell 调用
    run("npm install --no-audit --no-fund" if IS_WINDOWS else ["npm", "install", "--no-audit", "--no-fund"], cwd=frontend.directory)


def preflight(skip_install: bool) -> tuple[str | None, str]:
    uv = find_uv()
    node = find_executable("node")
    if not node or not find_executable("npm"):
        raise LauncherError("没有找到 Node.js（node / npm）。请先安装 Node.js 20.19 或更高版本： https://nodejs.org/")
    version = subprocess.run([node, "--version"], capture_output=True, text=True).stdout.strip().lstrip("v")
    major, minor = (int(part) for part in (version.split(".") + ["0", "0"])[:2])
    if (major, minor) < (20, 19) or major == 21 or (major == 22 and minor < 12):
        say(f"  [注意] 当前 Node.js 版本为 {version}，前端使用的 Vite 需要 20.19+ 或 22.12+，如启动失败请升级 Node.js。")
    if not skip_install and not uv and sys.version_info < (3, 12):
        raise LauncherError(
            "没有找到 uv，且当前 Python 版本低于 3.12。请先安装 uv： https://docs.astral.sh/uv/getting-started/installation/"
        )
    return uv, node


# --------------------------------------------------------------------------- Docker 基础设施（PostgreSQL + Redis）


def start_docker_infra() -> None:
    docker = find_executable("docker")
    if not docker:
        raise LauncherError("没有找到 docker 命令。请先安装并启动 Docker Desktop，或去掉 --docker 使用默认的零依赖模式。")
    if subprocess.run([docker, "info"], capture_output=True).returncode != 0:
        raise LauncherError("Docker 没有在运行。请先启动 Docker Desktop，等它就绪后再试。")
    compose_file = ROOT / "deploy" / "compose.yml"
    say("  启动 PostgreSQL（pgvector）与 Redis 容器 …")
    run([docker, "compose", "-f", str(compose_file), "up", "-d", "--wait"], cwd=ROOT / "deploy")
    for title, port in (("PostgreSQL", PG_PORT), ("Redis", REDIS_PORT)):
        deadline = time.monotonic() + 60
        while not port_in_use(port):
            if time.monotonic() > deadline:
                raise LauncherError(f"{title} 没有在端口 {port} 上就绪，请查看：docker compose -f deploy/compose.yml logs")
            time.sleep(1)
        say(f"  {title} 已就绪（127.0.0.1:{port}）")


# --------------------------------------------------------------------------- 进程管理


def spawn(command: list[str], cwd: Path, log_name: str, env: dict[str, str], detach: bool) -> subprocess.Popen:
    LOG_DIR.mkdir(exist_ok=True)
    log_file = (LOG_DIR / f"{log_name}.log").open("ab")
    log_file.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} 启动 =====\n".encode())
    log_file.flush()
    options: dict = {"cwd": cwd, "env": env, "stdin": subprocess.DEVNULL, "stdout": log_file, "stderr": subprocess.STDOUT}
    if IS_WINDOWS:
        # 独立进程组：在启动器窗口按 Ctrl+C 时由启动器统一、有序地停止各应用
        flags = subprocess.CREATE_NEW_PROCESS_GROUP
        if detach:
            flags |= subprocess.DETACHED_PROCESS
        options["creationflags"] = flags
    else:
        options["start_new_session"] = True
    return subprocess.Popen(command, **options)  # noqa: S603 - 命令由本脚本构造


def kill_tree(pid: int) -> None:
    if IS_WINDOWS:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
        return
    try:
        os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.killpg(pid, 0)
        except (ProcessLookupError, PermissionError):
            return
        time.sleep(0.2)
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def save_pids(processes: dict[str, subprocess.Popen]) -> None:
    RUNTIME_DIR.mkdir(exist_ok=True)
    PID_FILE.write_text(json.dumps({name: process.pid for name, process in processes.items()}, indent=2), encoding="utf-8")


def stop_all(quiet: bool = False) -> int:
    if not PID_FILE.exists():
        if not quiet:
            say("没有找到运行记录（.runtime/pids.json），当前没有由启动器启动的应用。")
        return 0
    try:
        pids: dict[str, int] = json.loads(PID_FILE.read_text(encoding="utf-8"))
    except ValueError:
        pids = {}
    for name, pid in pids.items():
        kill_tree(int(pid))
        if not quiet:
            say(f"  已停止 {name}（PID {pid}）")
    PID_FILE.unlink(missing_ok=True)
    return len(pids)


def wait_until_ready(processes: dict[str, subprocess.Popen], timeout: float = 120.0) -> None:
    pending: dict[str, tuple[str, int, bool]] = {
        **{backend.name: (backend.title, backend.port, True) for backend in BACKENDS},
        **{frontend.name: (frontend.title, frontend.port, False) for frontend in FRONTENDS},
    }
    deadline = time.monotonic() + timeout
    while pending:
        for name, (title, port, is_backend) in list(pending.items()):
            code = processes[name].poll()
            if code is not None:
                raise LauncherError(
                    f"{title}（{name}）启动失败，退出码 {code}。日志 logs/{name}.log 的最后几行：\n\n{tail(LOG_DIR / f'{name}.log')}"
                )
            ready = http_json(f"http://127.0.0.1:{port}/health") is not None if is_backend else port_in_use(port)
            if ready:
                say(f"  [OK] {title:<4} http://127.0.0.1:{port}")
                del pending[name]
        if pending and time.monotonic() > deadline:
            names = "、".join(title for title, _, _ in pending.values())
            raise LauncherError(f"等待超时：{names} 没有在 {int(timeout)} 秒内就绪，请查看 logs/ 目录下的日志。")
        time.sleep(0.5)


def print_summary(use_docker: bool) -> None:
    ai_health = http_json("http://127.0.0.1:8002/health") or {}
    engine = (
        f"大模型 {ai_health.get('engine')}（规则引擎兜底）"
        if ai_health.get("llm_enabled")
        else "内置规则引擎（在 ai-service/.env 中填写 LLM_API_KEY 后重启，即可启用大模型）"
    )
    storage = "PostgreSQL + Redis（Docker）" if use_docker else "SQLite + 进程内事件总线（零依赖）"
    say(
        f"""
============================================================
  电商小二 已启动
============================================================
  用户端（商城 / 智能客服）   http://127.0.0.1:5173
  管理端（人工工作台 / 监控） http://127.0.0.1:5174

  客服服务 :8000   电商服务 :8001   智能服务 :8002
  接口文档  http://127.0.0.1:8000/docs  （8001、8002 同理）

  数据存储：{storage}
  回答引擎：{engine}

  演示账号（页面右上角切换，无需密码）
    用户端：李明 user_001 / 王芳 user_002
    管理端：客服小周 agent_001（人工工作台）/ 系统管理员 admin_001（监控与知识库）

  日志目录：logs/        全链路自检：test.bat
============================================================"""
    )


# --------------------------------------------------------------------------- 子命令


def command_start(args: argparse.Namespace) -> int:
    step("检查运行环境")
    uv, node = preflight(args.skip_install)
    say(f"  uv：{uv or '未安装（改用 Python 自带的 venv + pip）'}")
    say(f"  node：{node}")

    if stop_all(quiet=True):
        say("  已停止上一次启动的应用")
        time.sleep(1)
    busy = [
        f"{item.title} :{item.port}" for item in (*BACKENDS, *FRONTENDS) if port_in_use(item.port)
    ]
    if busy:
        raise LauncherError(
            "以下端口已被其他程序占用：" + "、".join(busy) + "\n  请先关闭占用端口的程序（或运行 stop.bat）后重试。"
        )

    for item in (*BACKENDS, *FRONTENDS):
        ensure_env_file(item.directory)

    if not args.skip_install:
        step("安装依赖（首次运行需要下载，可能要几分钟）")
        for backend in BACKENDS:
            install_backend(backend, uv, args.mirror)
        for frontend in FRONTENDS:
            install_frontend(frontend)
    for backend in BACKENDS:
        if not backend.python.exists():
            raise LauncherError(f"{backend.title} 的虚拟环境不存在：{backend.python}\n  请去掉 --skip-install 重新运行。")

    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    if args.docker:
        step("启动基础设施：PostgreSQL + Redis（Docker）")
        start_docker_infra()
        env.update(DOCKER_ENV)

    step("启动应用")
    processes: dict[str, subprocess.Popen] = {}
    try:
        for backend in BACKENDS:
            processes[backend.name] = spawn(
                [str(backend.python), "-m", backend.module], backend.directory, backend.name, env, args.detach
            )
        for frontend in FRONTENDS:
            vite = frontend.directory / "node_modules" / "vite" / "bin" / "vite.js"
            processes[frontend.name] = spawn(
                [node, str(vite), "--host", "127.0.0.1", "--port", str(frontend.port), "--strictPort"],
                frontend.directory,
                frontend.name,
                env,
                args.detach,
            )
        save_pids(processes)
        wait_until_ready(processes)
    except BaseException:
        for process in processes.values():
            kill_tree(process.pid)
        PID_FILE.unlink(missing_ok=True)
        raise

    print_summary(args.docker)
    if not args.no_browser:
        webbrowser.open("http://127.0.0.1:5173/chat")
        webbrowser.open("http://127.0.0.1:5174")

    if args.detach:
        say("\n应用在后台运行。停止请运行 stop.bat（或 python scripts/xiaoer.py stop）。")
        return 0

    say("\n保持这个窗口打开；按 Ctrl+C 或直接关闭窗口即可停止全部应用。")
    try:
        while True:
            time.sleep(1)
            for name, process in processes.items():
                code = process.poll()
                if code is not None:
                    say(f"\n[!!] {name} 意外退出（退出码 {code}）。日志 logs/{name}.log 的最后几行：\n\n{tail(LOG_DIR / f'{name}.log')}")
                    raise LauncherError("有应用意外退出，已停止其余应用。")
    except KeyboardInterrupt:
        say("\n正在停止全部应用 …")
        return 0
    finally:
        for process in processes.values():
            kill_tree(process.pid)
        PID_FILE.unlink(missing_ok=True)


def command_stop(_: argparse.Namespace) -> int:
    stopped = stop_all()
    if stopped:
        say(f"已停止 {stopped} 个应用。")
    say("提示：使用 --docker 启动的 PostgreSQL / Redis 容器仍在运行，如需停止：docker compose -f deploy/compose.yml down")
    return 0


def command_status(_: argparse.Namespace) -> int:
    for backend in BACKENDS:
        health = http_json(f"http://127.0.0.1:{backend.port}/health")
        detail = "、".join(f"{key}={value}" for key, value in (health or {}).items() if key not in ("status", "service"))
        say(f"  [{'运行中' if health else '未启动'}] {backend.title:<4} :{backend.port}  {detail}")
    for frontend in FRONTENDS:
        say(f"  [{'运行中' if port_in_use(frontend.port) else '未启动'}] {frontend.title:<4} :{frontend.port}")
    return 0


def command_test(_: argparse.Namespace) -> int:
    python = BACKENDS[2].python  # 自检脚本依赖 httpx 与 websockets，客服服务的虚拟环境里都有
    if not python.exists():
        raise LauncherError("还没有安装依赖，请先运行 start.bat 启动一次。")
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    return subprocess.run([str(python), str(ROOT / "scripts" / "smoke_test.py")], cwd=ROOT, env=env).returncode


def command_reset(args: argparse.Namespace) -> int:
    if any(port_in_use(backend.port) for backend in BACKENDS):
        raise LauncherError("后端服务还在运行，请先停止（stop.bat）再重置数据。")
    if not args.yes:
        answer = input("将删除三个后端的本地 SQLite 数据（会话、工单、AI 运行记录、上传的知识库），确认？[y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            say("已取消。")
            return 0
    for backend in BACKENDS:
        data_dir = backend.directory / "data"
        if data_dir.exists():
            shutil.rmtree(data_dir)
            say(f"  已删除 {data_dir.relative_to(ROOT)}")
    say("本地数据已清空，下次启动时会自动重建演示数据与内置知识库。")
    say("提示：--docker 模式的数据保存在 Docker 卷中，重置方法：docker compose -f deploy/compose.yml down -v")
    return 0


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")

    parser = argparse.ArgumentParser(prog="xiaoer", description="电商小二 · 本地启动器")
    commands = parser.add_subparsers(dest="command", required=True)

    start = commands.add_parser("start", help="启动五个应用")
    start.add_argument("--docker", action="store_true", help="使用 PostgreSQL + Redis（由 Docker 启动），而不是默认的 SQLite")
    start.add_argument("--mirror", nargs="?", const=DEFAULT_MIRROR, default=None, metavar="URL", help="安装 Python 依赖时使用国内镜像（默认阿里云）")
    start.add_argument("--skip-install", action="store_true", help="跳过依赖安装（已安装过时启动更快）")
    start.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    start.add_argument("--detach", action="store_true", help="启动后返回，应用留在后台运行")
    start.set_defaults(handler=command_start)

    commands.add_parser("stop", help="停止应用").set_defaults(handler=command_stop)
    commands.add_parser("status", help="查看运行状态").set_defaults(handler=command_status)
    commands.add_parser("test", help="运行全链路自检").set_defaults(handler=command_test)
    reset = commands.add_parser("reset", help="清空本地 SQLite 数据")
    reset.add_argument("-y", "--yes", action="store_true", help="不询问，直接清空")
    reset.set_defaults(handler=command_reset)

    args = parser.parse_args()
    try:
        return args.handler(args)
    except LauncherError as error:
        say(f"\n[错误] {error}")
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
