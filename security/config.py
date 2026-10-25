"""环境配置。密钥只从环境变量或 .env 读取，不进入代码与日志。"""

import os
import sys
from pathlib import Path

APP_NAME = "Blossom"
# 显示名换成 Blossom，但用户数据目录名必须保持原样：
# 改名不能把已有的数据库、记忆和配置弄丢。
DATA_DIR_NAME = "DeskPet"


def program_dir():
    """程序目录。打包成 EXE 后是 EXE 所在目录，不是临时解包目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


PROGRAM_DIR = program_dir()
PROJECT_ROOT = PROGRAM_DIR

SECRET_ENV_NAMES = (
    "DESKPET_LLM_API_KEY",
    "AGENT_LLM_API_KEY",
    "OPENAI_API_KEY",
    "PY_LLM_API_KEY",
)

NEWS_SECRET_ENV_NAMES = (
    "DESKPET_NEWS_API_KEY",
    "NEWS_API_KEY",
)

COMPANION_SECRET_ENV_NAMES = ("DESKPET_COMPANION_API_KEY",)
TASK_SECRET_ENV_NAMES = ("DESKPET_TASK_API_KEY",)


# 进程刚起来时系统里就有的环境变量。它们的优先级最高，.env 不许盖掉。
# 在模块导入时记一次，避免同一进程里第二次装配置时读到的「已有变量」其实是上一次读进来的。
PRESET_ENV = frozenset(os.environ)


def load_dotenv(path=None, override=False, keep=()):
    """读取 .env 到环境变量，默认不覆盖已经存在的变量。

    override 用在用户数据目录那一份配置上。打包后程序目录里那份模板会先被读一遍，
    如果模板优先级更高，用户自己填的配置就永远不生效（模型名、开关都会被挡住）。
    所以用户这一份可以顶掉前面读到的值。keep 里列出的是系统里本来就有的环境变量，
    它们优先级最高，.env 不能盖掉。
    """
    env_path = Path(path) if path else PROGRAM_DIR / ".env"
    if not env_path.is_file():
        return False
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip()
        if not name or name in keep:
            continue
        if override or name not in os.environ:
            os.environ[name] = value.strip().strip('"').strip("'")
    return True


def _env(*names, default=""):
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return default


def _flag(name, default=False):
    value = (os.environ.get(name) or "").strip()
    if not value:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def data_root(program=None):
    """用户数据根目录。

    开发时就是项目目录（data/、logs/ 位置不变）。
    打包后放到 Windows 用户数据目录：重新打包不会误删用户数据，
    程序放在 Program Files 这类只读位置也能正常读写。
    """
    override = (os.environ.get("DESKPET_ROOT") or "").strip()
    if override:
        return Path(override).expanduser().resolve()
    if getattr(sys, "frozen", False):
        base = os.environ.get("LOCALAPPDATA") or str(Path.home())
        return (Path(base) / DATA_DIR_NAME).resolve()
    return Path(program or PROGRAM_DIR)


def _path(anchor, name, default):
    """相对路径一律按数据根目录解析，避免从别处启动时写到别的目录。"""
    path = Path(_env(name, default=str(default)))
    return path if path.is_absolute() else (Path(anchor) / path).resolve()


def resource_path(name):
    """只读资源。打包后资源在解包目录里，开发时在项目 resources/ 下。"""
    base = getattr(sys, "_MEIPASS", None)
    root = Path(base) if base else PROGRAM_DIR
    return root / "resources" / name


class Config:
    """运行配置。全部有默认值，缺省即可在本地启动。"""

    def __init__(self, load_env=True):
        if load_env:
            load_dotenv()

        self.program_dir = PROGRAM_DIR
        self.frozen = bool(getattr(sys, "frozen", False))
        # 打包后数据在用户目录，重新打包不会碰到；开发时仍在项目目录。
        self.data_root = data_root(PROGRAM_DIR)
        if load_env and self.data_root != self.program_dir:
            # 用户自己的配置压过程序目录里的模板（模板只是默认值）。
            load_dotenv(self.data_root / ".env", override=True, keep=PRESET_ENV)
        self.project_root = self.data_root
        self.data_dir = _path(self.data_root, "DESKPET_DATA_DIR", self.data_root / "data")
        self.log_dir = _path(self.data_root, "DESKPET_LOG_DIR", self.data_root / "logs")
        self.db_path = _path(
            self.data_root, "DESKPET_DB_PATH", self.data_dir / "deskpet.sqlite3"
        )
        # 角色包目录：用户自己放人格 / 形象的地方，不放任何密钥。
        self.characters_dir = _path(
            self.data_root, "DESKPET_CHARACTERS_DIR", self.data_root / "characters"
        )
        # Live2D 模型目录：用户自己放模型的地方，程序不下载、不制作模型。
        self.live2d_dir = _path(
            self.data_root, "DESKPET_LIVE2D_DIR", self.data_root / "live2d"
        )
        self.live2d_model = _env("DESKPET_LIVE2D_MODEL")
        # 角色画面：开了就在独立透明窗口里画；缺渲染环境时自动退回占位形象。
        # 渲染器只读模型文件、只画画面，不执行模型包里的任何脚本。
        self.live2d_render = _flag("DESKPET_LIVE2D_RENDER", True)
        self.live2d_scale = self._bounded_float("DESKPET_LIVE2D_SCALE", 1.0, 0.2, 4.0)
        # 角色窗口高度（逻辑像素）。宽度按模型自身比例算，不拉伸。
        self.live2d_height = self._bounded_int("DESKPET_LIVE2D_HEIGHT", 320, 80, 1200)
        # 渲染器自带资源的位置，正常情况下不用改。
        runtime = _env("DESKPET_LIVE2D_RUNTIME")
        self.live2d_runtime_dir = Path(runtime).expanduser().resolve() if runtime else None
        # 设备清单目录：以后把自己准备的外设清单放进来就会被认出来。
        # 放在数据目录里而不是程序目录：避开源码目录，也不会被打进 EXE。
        self.devices_dir = _path(
            self.data_dir, "DESKPET_DEVICES_DIR", self.data_dir / "devices"
        )
        # 语音包目录：用户自己准备的语音包放这里，程序只读元数据，不执行任何代码。
        self.voices_dir = _path(
            self.data_dir, "DESKPET_VOICES_DIR", self.data_dir / "voices"
        )
        # Skill 目录：用户自己导入的 Skill 放这里，程序只读说明文件，不跑脚本。
        self.skills_dir = _path(
            self.data_dir, "DESKPET_SKILLS_DIR", self.data_dir / "skills"
        )
        # 工作区：工具允许读写的目录边界。默认就是项目目录，路径跑出去一律拒绝。
        workspace = _env("DESKPET_WORKSPACE")
        self.workspace_root = (
            Path(workspace).expanduser().resolve() if workspace else self.project_root
        )

        # 系统托盘与全局快捷键：托盘默认开（装不上就安静降级）；快捷键默认不占用。
        self.tray = _flag("DESKPET_TRAY", True)
        self.hotkey = _env("DESKPET_HOTKEY")
        self.log_level = _env("DESKPET_LOG_LEVEL", default="INFO").upper()
        self.dev_mode = _flag("DESKPET_DEV_MODE")
        # 语音输出：默认关闭；开了就用系统自带朗读念出来，不联网、不加依赖。
        self.voice_output = _flag("DESKPET_VOICE_OUTPUT")
        self.voice_rate = self._bounded_int("DESKPET_VOICE_RATE", 0, -10, 10)
        self.voice_volume = self._bounded_int("DESKPET_VOICE_VOLUME", 100, 0, 100)
        # 语音输入：识别引擎与本地模型都由用户自己准备，程序不下载、不安装。
        # 没装引擎或没配模型时保持不可用，只影响说话输入，不影响其它功能。
        self.stt_engine = _env("DESKPET_STT_ENGINE").strip().lower()
        self.stt_model = _env("DESKPET_STT_MODEL")
        # V9.1 第四阶段：离线识别用 sherpa-onnx 时，模型是哪一种、说什么语言。
        # auto 会让识别层按目录名和文件名认，认不出就逐个试。
        self.stt_kind = _env("DESKPET_STT_KIND", default="auto")
        self.stt_language = _env("DESKPET_STT_LANGUAGE")
        self.recordings_dir = _path(
            self.data_dir, "DESKPET_RECORDINGS_DIR", self.data_dir / "recordings"
        )
        # 屏幕感知：默认关着。打开之后本地工具才读前台窗口和公开的界面语义。
        # 不截图、不录键盘、不做身份推断，也不要用户为它装任何东西。
        self.screen_perception = _flag("DESKPET_SCREEN_PERCEPTION")

        self.llm_provider = _env("DESKPET_LLM_PROVIDER", default="openai-compatible")
        self.llm_base_url = _env("AGENT_LLM_BASE_URL", "OPENAI_BASE_URL", "DESKPET_LLM_BASE_URL")
        self.llm_model = _env("AGENT_LLM_MODEL", "OPENAI_MODEL", "DESKPET_LLM_MODEL")
        self.llm_api_key = _env(*SECRET_ENV_NAMES)
        if self.llm_base_url and not self.llm_api_key:
            self.llm_api_key = "local"
        self.llm_timeout = self._bounded_float("AGENT_LLM_TIMEOUT", 60.0, 5.0, 180.0)
        # 代理与重试：代理只影响模型请求；重试由适配层统一做，SDK 自己不重试。
        self.llm_proxy = _env("DESKPET_LLM_PROXY")
        self.llm_retries = self._bounded_int("DESKPET_LLM_RETRIES", 1, 0, 2)

        # 双模型：Companion 负责陪伴聊天，Task 只在真正执行任务时使用。
        # 新配置优先，旧 AGENT_LLM_* 继续作为 Companion 的 fallback，不强制迁移。
        self.companion_base_url = _env("DESKPET_COMPANION_BASE_URL") or self.llm_base_url
        self.companion_model = _env("DESKPET_COMPANION_MODEL") or self.llm_model
        self.companion_api_key = _env(*COMPANION_SECRET_ENV_NAMES) or self.llm_api_key
        if self.companion_base_url and not self.companion_api_key:
            self.companion_api_key = "local"

        self.task_base_url = _env("DESKPET_TASK_BASE_URL")
        self.task_model = _env("DESKPET_TASK_MODEL")
        self.task_api_key = _env(*TASK_SECRET_ENV_NAMES)
        if self.task_base_url and not self.task_api_key:
            self.task_api_key = "local"
        # 需要确认的任务必须经过 TASK_PENDING。默认开启，可显式关闭。
        self.task_confirm = _flag("DESKPET_TASK_CONFIRM", True)

        # 模型配置仓库与密钥库：只存非敏感配置 + DPAPI 密文，都在数据目录下。
        from model.config import ModelConfigRepository
        from security.credentials import SecretStore

        self.model_configs = ModelConfigRepository(self.data_dir)
        self.secrets = SecretStore(self.data_dir / "secrets")

        # 上下文与记忆的预算控制，避免把全部历史发给模型。
        self.context_budget = self._bounded_int("DESKPET_CONTEXT_BUDGET", 2000, 200, 32000)
        # 近期候选消息：20-30 条起步，再按真实 token 预算裁剪。
        self.recent_window = self._bounded_int("DESKPET_RECENT_WINDOW", 24, 2, 200)
        # 每轮检索多少条相关记忆（和总存储量分开）。
        self.memory_limit = self._bounded_int("DESKPET_MEMORY_LIMIT", 12, 0, 50)
        # 摘要触发阈值与摘要长度上限。
        self.summary_trigger = self._bounded_int("DESKPET_SUMMARY_TRIGGER", 24, 4, 500)
        self.summary_max_chars = self._bounded_int(
            "DESKPET_SUMMARY_MAX_CHARS", 600, 100, 4000
        )
        # 记忆容量策略：超出就归档最旧最不重要的，不删除。锁定项不受限。
        self.memory_capacity = self._bounded_int(
            "DESKPET_MEMORY_CAPACITY", 2000, 50, 100000
        )

        # 主动陪伴：默认开启，但只在规则命中时触发，不做模型轮询。
        self.proactive = _flag("DESKPET_PROACTIVE", True)
        self.work_reminder_seconds = self._bounded_int(
            "DESKPET_WORK_REMINDER_SECONDS", 3600, 60, 86400
        )
        # 用户阀门：0 表示不加额外限制。界面里改的是偏好，重启后仍然算数。
        self.proactive_cooldown = self._bounded_int(
            "DESKPET_PROACTIVE_COOLDOWN", 0, 0, 86400
        )
        self.proactive_max_per_day = self._bounded_int(
            "DESKPET_PROACTIVE_MAX_PER_DAY", 0, 0, 100
        )
        # V3：主动话题默认允许，但每轮最多说一条，用户随时可以关掉。
        self.proactive_topics = _flag("DESKPET_PROACTIVE_TOPICS", True)
        # 一次 tick 最多开口几条。多条同时命中时按优先级排队，剩下的下一轮再说。
        self.proactive_max_per_tick = self._bounded_int(
            "DESKPET_PROACTIVE_MAX_PER_TICK", 1, 1, 10
        )

        # 外部服务：密钥只从环境变量读取，未配置时服务如实报不可用。
        self.weather_city = _env("DESKPET_WEATHER_CITY")
        self.news_api_key = _env(*NEWS_SECRET_ENV_NAMES)
        self.news_base_url = _env("DESKPET_NEWS_BASE_URL")
        self.service_cache_ttl = self._bounded_int("DESKPET_SERVICE_CACHE_TTL", 600, 0, 86400)

        # 人格：名字与性格强度（如 理性:70,温柔:50）。只影响表达，不影响权限。
        self.persona_name = _env("DESKPET_PERSONA_NAME", default="小蝶")
        self.persona_traits = _env("DESKPET_PERSONA_TRAITS")
        self.character = _env("DESKPET_CHARACTER")

        # 外部设备：当前默认只允许模拟设备，真实硬件需显式打开。
        self.allow_real_devices = _flag("DESKPET_ALLOW_REAL_DEVICES")

        self.ensure_dirs()

    def secret_values(self):
        """需要脱敏的值。只用于日志过滤，不用于展示。"""
        values = [
            value
            for value in (
                self.llm_api_key,
                self.companion_api_key,
                self.task_api_key,
                self.news_api_key,
            )
            if value and value != "local"
        ]
        # 用户从界面存进 DPAPI 的 Key 也要进脱敏表，否则日志里出现同样字符串时挡不住。
        store = getattr(self, "secrets", None)
        repo = getattr(self, "model_configs", None)
        if store is not None and repo is not None:
            try:
                for row in repo.list():
                    key = store.load(row.get("secret_id"))
                    if key:
                        values.append(key)
            except Exception:
                pass
        return tuple(dict.fromkeys(values))

    @staticmethod
    def _bounded_float(name, default, low, high):
        try:
            return max(low, min(high, float(os.environ.get(name) or default)))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _bounded_int(name, default, low, high):
        try:
            return max(low, min(high, int(os.environ.get(name) or default)))
        except (TypeError, ValueError):
            return default

    def ensure_dirs(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        # 提前把放模型的位置建好，用户只要把 Live2D 文件夹拖进去。
        self.live2d_dir.mkdir(parents=True, exist_ok=True)
        # 设备清单目录同理：现在只是一个空目录。
        self.devices_dir.mkdir(parents=True, exist_ok=True)
        # 语音包目录：用户把语音包拖进来就能被认出来。
        self.voices_dir.mkdir(parents=True, exist_ok=True)
        # Skill 目录同理：先建好，用户把 Skill 文件夹放进去就能被认出来。
        self.skills_dir.mkdir(parents=True, exist_ok=True)

    def has_llm(self):
        return bool(self.llm_model)

    def _model_default(self, role):
        """配置仓库里的默认项；仓库不可用时返回 None。"""
        repo = getattr(self, "model_configs", None)
        if repo is None:
            return None
        try:
            return repo.default(role)
        except Exception:
            return None

    def has_companion(self):
        return bool(self._model_default("companion") or self.companion_model)

    def has_task(self):
        return bool(self._model_default("task") or self.task_model)

    def _has_stored_key(self):
        repo = getattr(self, "model_configs", None)
        store = getattr(self, "secrets", None)
        if repo is None or store is None:
            return False
        try:
            return any(store.has(row.get("secret_id")) for row in repo.list())
        except Exception:
            return False

    def redacted(self):
        """日志与开发者模式使用的安全视图，永远不含密钥明文。"""
        companion = self._model_default("companion") or {}
        task = self._model_default("task") or {}
        stored = self._has_stored_key()
        return {
            "project_root": str(self.project_root),
            "program_dir": str(self.program_dir),
            "frozen": self.frozen,
            "db_path": str(self.db_path),
            "log_dir": str(self.log_dir),
            "devices_dir": str(self.devices_dir),
            "skills_dir": str(self.skills_dir),
            "workspace_root": str(self.workspace_root),
            "live2d_dir": str(self.live2d_dir),
            "live2d_model": self.live2d_model or "(unset)",
            "live2d_render": self.live2d_render,
            "live2d_scale": self.live2d_scale,
            "log_level": self.log_level,
            "dev_mode": self.dev_mode,
            "voice_output": self.voice_output,
            "voice_rate": self.voice_rate,
            "voice_volume": self.voice_volume,
            "voices_dir": str(self.voices_dir),
            "stt_engine": self.stt_engine or "(unset)",
            "screen_perception": self.screen_perception,
            "stt_model": self.stt_model or "(unset)",
            "recordings_dir": str(self.recordings_dir),
            "tray": self.tray,
            "hotkey": self.hotkey or "(unset)",
            "llm_provider": self.llm_provider,
            "llm_base_url": self.llm_base_url or "(unset)",
            "llm_model": self.llm_model or "(unset)",
            "llm_api_key": "set" if self.llm_api_key else "unset",
            "companion_base_url": companion.get("base_url")
            or self.companion_base_url
            or "(unset)",
            "companion_model": companion.get("model")
            or self.companion_model
            or "(unset)",
            "companion_api_key": "set"
            if (stored or self.companion_api_key)
            else "unset",
            "task_base_url": task.get("base_url") or self.task_base_url or "(unset)",
            "task_model": task.get("model") or self.task_model or "(unset)",
            "task_api_key": "set" if (stored or self.task_api_key) else "unset",
            "task_confirm": self.task_confirm,
            "weather_city": self.weather_city or "(unset)",
            "news_api_key": "set" if self.news_api_key else "unset",
            "service_cache_ttl": self.service_cache_ttl,
            "allow_real_devices": self.allow_real_devices,
            "proactive": self.proactive,
            "proactive_cooldown": self.proactive_cooldown,
            "proactive_max_per_day": self.proactive_max_per_day,
            "proactive_topics": self.proactive_topics,
            "proactive_max_per_tick": self.proactive_max_per_tick,
            "context_budget": self.context_budget,
            "recent_window": self.recent_window,
            "memory_limit": self.memory_limit,
            "summary_trigger": self.summary_trigger,
            "summary_max_chars": self.summary_max_chars,
            "memory_capacity": self.memory_capacity,
        }
