"""内置常用环境变量目录。

目录只描述变量名、分类、用途与示例，不保存任何真实值。敏感标记由变量名自动推断
（与 :func:`envforge.models.looks_sensitive_key` 保持一致），用于在界面与导出时提醒
用户按密钥处理。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .models import looks_sensitive_key

CAT_RUNTIME = "运行时与工具链"
CAT_PATH = "路径与缓存"
CAT_PROXY = "代理与网络"
CAT_BUILD = "构建与编译"
CAT_DEBUG = "调试与日志"
CAT_TERM = "终端与本地化"
CAT_CLOUD = "容器与云"
CAT_DB = "数据库"
CAT_APP = "应用与密钥"
CAT_WIN = "Windows 系统"

CATEGORIES = (
    CAT_RUNTIME,
    CAT_PATH,
    CAT_PROXY,
    CAT_BUILD,
    CAT_DEBUG,
    CAT_TERM,
    CAT_CLOUD,
    CAT_DB,
    CAT_APP,
    CAT_WIN,
)

_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True, slots=True)
class EnvPreset:
    key: str
    category: str
    description: str
    example: str = ""
    note: str = ""
    sensitive: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "category": self.category,
            "description": self.description,
            "example": self.example,
            "note": self.note,
            "sensitive": self.sensitive,
        }


def _p(key: str, category: str, description: str, example: str = "", note: str = "") -> EnvPreset:
    return EnvPreset(
        key=key,
        category=category,
        description=description,
        example=example,
        note=note,
        sensitive=looks_sensitive_key(key),
    )


PRESETS: tuple[EnvPreset, ...] = (
    # ------------------------------------------------------------ 运行时与工具链
    _p("JAVA_HOME", CAT_RUNTIME, "JDK 安装根目录；Maven、Gradle 与多数 IDE 依赖它定位 Java。", r"C:\Program Files\Java\jdk-21"),
    _p("JRE_HOME", CAT_RUNTIME, "JRE 根目录（旧版工具链使用）；新项目通常只需 JAVA_HOME。", r"C:\Program Files\Java\jre-21"),
    _p("MAVEN_HOME", CAT_RUNTIME, "Maven 安装目录，供脚本定位 mvn 与配置。", r"C:\apache-maven-3.9"),
    _p("GRADLE_USER_HOME", CAT_RUNTIME, "Gradle 用户缓存与全局配置目录，可迁移到大容量磁盘。", r"%USERPROFILE%\.gradle"),
    _p("ANT_HOME", CAT_RUNTIME, "Apache Ant 安装目录（遗留 Java 项目常用）。", r"C:\apache-ant-1.10"),
    _p("CATALINA_HOME", CAT_RUNTIME, "Tomcat 安装目录；启动脚本依赖它定位 lib 与 bin。", r"C:\apache-tomcat-10"),
    _p("PYTHONHOME", CAT_RUNTIME, "Python 标准库根目录；多版本隔离时显式指定，避免串版本。", r"C:\Python312"),
    _p("PYTHONPATH", CAT_RUNTIME, "额外的模块搜索路径，多个目录用分号分隔（可包含 %VAR%）。", r"%USERPROFILE%\pylib"),
    _p("PYTHONSTARTUP", CAT_RUNTIME, "交互式解释器启动时自动执行的脚本。", r"%USERPROFILE%\.pythonstartup.py"),
    _p("PYTHONUSERBASE", CAT_RUNTIME, "pip install --user 的安装根目录。", r"%USERPROFILE%\.local"),
    _p("PYTHONIOENCODING", CAT_RUNTIME, "强制标准输入输出编码，避免 Windows 控制台中文乱码。", "utf-8"),
    _p("PIP_CACHE_DIR", CAT_RUNTIME, "pip 下载缓存目录，清理可释放磁盘空间。", r"%LOCALAPPDATA%\pip\Cache"),
    _p("PIP_CONFIG_FILE", CAT_RUNTIME, "pip 配置文件路径，用于固定镜像源与超时。", r"%USERPROFILE%\pip\pip.ini"),
    _p("VIRTUAL_ENV", CAT_RUNTIME, "当前激活的虚拟环境目录；由 venv 激活脚本自动设置。", r"D:\work\demo\.venv"),
    _p("CONDA_PREFIX", CAT_RUNTIME, "当前激活的 Conda 环境目录；由 conda activate 自动设置。", r"%USERPROFILE%\miniconda3\envs\dev"),
    _p("NODE_PATH", CAT_RUNTIME, "Node 额外的模块搜索路径（历史用法，现代项目更推荐本地依赖）。", r"%USERPROFILE%\node_modules"),
    _p("NODE_OPTIONS", CAT_RUNTIME, "Node 启动参数，例如限制堆大小或加载实验特性。", "--max-old-space-size=4096"),
    _p("NPM_CONFIG_PREFIX", CAT_RUNTIME, "npm 全局安装目录（npm i -g 的目标）。", r"%APPDATA%\npm"),
    _p("NPM_CONFIG_CACHE", CAT_RUNTIME, "npm 缓存目录，可迁移以释放系统盘。", r"%LOCALAPPDATA%\npm-cache"),
    _p("PNPM_HOME", CAT_RUNTIME, "pnpm 全局可执行文件目录，需要加入 PATH。", r"%LOCALAPPDATA%\pnpm"),
    _p("YARN_CACHE_FOLDER", CAT_RUNTIME, "Yarn 缓存目录。", r"%LOCALAPPDATA%\Yarn\Cache"),
    _p("DENO_DIR", CAT_RUNTIME, "Deno 缓存与模块存储目录。", r"%LOCALAPPDATA%\deno"),
    _p("BUN_INSTALL", CAT_RUNTIME, "Bun 安装根目录。", r"%USERPROFILE%\.bun"),
    _p("GOPATH", CAT_RUNTIME, "Go 工作区目录（模块模式下主要用于缓存与 bin）。", r"%USERPROFILE%\go"),
    _p("GOROOT", CAT_RUNTIME, "Go 安装根目录；通常由安装器设置，多版本时显式指定。", r"C:\Go"),
    _p("GOBIN", CAT_RUNTIME, "go install 生成的可执行文件目录。", r"%USERPROFILE%\go\bin"),
    _p("GOFLAGS", CAT_RUNTIME, "Go 命令默认参数，例如 -mod=mod 或代理行为。", "-mod=mod"),
    _p("CARGO_HOME", CAT_RUNTIME, "Cargo 主目录（缓存、registry 与已安装二进制）。", r"%USERPROFILE%\.cargo"),
    _p("RUSTUP_HOME", CAT_RUNTIME, "rustup 工具链与组件目录。", r"%USERPROFILE%\.rustup"),
    _p("GEM_HOME", CAT_RUNTIME, "Ruby gem 安装目录。", r"%USERPROFILE%\.gem"),
    _p("BUNDLE_PATH", CAT_RUNTIME, "Bundler 依赖安装目录。", r"D:\work\demo\vendor\bundle"),
    _p("RUBYLIB", CAT_RUNTIME, "Ruby 额外加载路径。", r"D:\work\demo\lib"),
    _p("PHP_INI_SCAN_DIR", CAT_RUNTIME, "PHP 额外 ini 扫描目录，用于加载扩展配置。", r"C:\php\conf.d"),
    _p("COMPOSER_HOME", CAT_RUNTIME, "Composer 全局配置与缓存目录。", r"%APPDATA%\Composer"),
    _p("DOTNET_ROOT", CAT_RUNTIME, ".NET 运行时/ SDK 根目录，多版本并存时显式指定。", r"C:\Program Files\dotnet"),
    _p("DOTNET_CLI_TELEMETRY_OPTOUT", CAT_RUNTIME, "关闭 .NET CLI 遥测。", "1"),
    _p("DOTNET_NOLOGO", CAT_RUNTIME, "抑制 .NET CLI 首次运行的欢迎信息。", "1"),
    _p("FLUTTER_ROOT", CAT_RUNTIME, "Flutter SDK 根目录。", r"D:\sdk\flutter"),
    _p("ANDROID_HOME", CAT_RUNTIME, "Android SDK 根目录（新版工具链推荐）。", r"%LOCALAPPDATA%\Android\Sdk"),
    _p("ANDROID_SDK_ROOT", CAT_RUNTIME, "Android SDK 根目录（旧名，部分工具仍读取）。", r"%LOCALAPPDATA%\Android\Sdk"),
    _p("ANDROID_NDK_HOME", CAT_RUNTIME, "Android NDK 目录，用于原生构建。", r"%LOCALAPPDATA%\Android\Sdk\ndk\27.0.12077973"),
    _p("CMAKE_PREFIX_PATH", CAT_RUNTIME, "CMake 查找依赖包时额外搜索的目录，多个用分号分隔。", r"C:\Qt\6.7\msvc2022_64"),
    _p("VCPKG_ROOT", CAT_RUNTIME, "vcpkg 安装根目录。", r"D:\tools\vcpkg"),
    _p("QTDIR", CAT_RUNTIME, "Qt 安装目录，qmake 与部分构建脚本会读取。", r"C:\Qt\6.7\msvc2022_64"),

    # ------------------------------------------------------------ 路径与缓存
    _p("PATH", CAT_PATH, "可执行文件搜索路径；建议用本工具「系统环境」页分析重复/失效段后再修改。", r"%USERPROFILE%\bin;%APPDATA%\npm"),
    _p("PATHEXT", CAT_PATH, "可执行扩展名列表，决定命令补全 .exe/.cmd 的方式。", ".COM;.EXE;.BAT;.CMD;.VBS"),
    _p("TEMP", CAT_PATH, "临时文件目录，构建与安装器大量使用。", r"%LOCALAPPDATA%\Temp"),
    _p("TMP", CAT_PATH, "临时文件目录别名，通常与 TEMP 保持一致。", r"%LOCALAPPDATA%\Temp"),
    _p("TMPDIR", CAT_PATH, "Unix 风格工具的临时目录变量。", r"%LOCALAPPDATA%\Temp"),
    _p("USERPROFILE", CAT_PATH, "当前用户主目录。", r"C:\Users\me"),
    _p("APPDATA", CAT_PATH, "当前用户的漫游应用数据目录。", r"%USERPROFILE%\AppData\Roaming"),
    _p("LOCALAPPDATA", CAT_PATH, "当前用户的本地应用数据目录（缓存与大文件通常放这里）。", r"%USERPROFILE%\AppData\Local"),
    _p("PROGRAMDATA", CAT_PATH, "所有用户共享的应用数据目录。", r"C:\ProgramData"),
    _p("PROGRAMFILES", CAT_PATH, "64 位程序安装目录。", r"C:\Program Files"),
    _p("XDG_CONFIG_HOME", CAT_PATH, "跨平台工具读取的配置文件目录。", r"%USERPROFILE%\.config"),
    _p("XDG_CACHE_HOME", CAT_PATH, "跨平台工具读取的缓存目录。", r"%USERPROFILE%\.cache"),
    _p("XDG_DATA_HOME", CAT_PATH, "跨平台工具读取的数据目录。", r"%USERPROFILE%\.local\share"),

    # ------------------------------------------------------------ 代理与网络
    _p("HTTP_PROXY", CAT_PROXY, "HTTP 代理地址，命令行工具普遍读取。", "http://127.0.0.1:7890", note="部分工具要求大写与小写同时设置。"),
    _p("HTTPS_PROXY", CAT_PROXY, "HTTPS 代理地址。", "http://127.0.0.1:7890"),
    _p("ALL_PROXY", CAT_PROXY, "所有协议的兜底代理（curl/部分运行时支持）。", "socks5://127.0.0.1:1080"),
    _p("NO_PROXY", CAT_PROXY, "不经过代理的主机列表，逗号分隔。", "localhost,127.0.0.1,*.local"),
    _p("PIP_INDEX_URL", CAT_PROXY, "pip 默认索引源，可替换为内网镜像。", "https://pypi.tuna.tsinghua.edu.cn/simple"),
    _p("PIP_EXTRA_INDEX_URL", CAT_PROXY, "pip 额外索引源，用于私有包。", "https://pypi.example.com/simple"),
    _p("PIP_TRUSTED_HOST", CAT_PROXY, "允许 pip 访问的自签名 HTTPS 主机。", "pypi.example.com"),
    _p("NPM_CONFIG_REGISTRY", CAT_PROXY, "npm 包源地址。", "https://registry.npmmirror.com"),
    _p("YARN_REGISTRY", CAT_PROXY, "Yarn 包源地址。", "https://registry.npmmirror.com"),
    _p("NODE_TLS_REJECT_UNAUTHORIZED", CAT_PROXY, "设为 0 会关闭 Node TLS 证书校验，仅限临时排错。", "0", note="关闭证书校验会显著降低安全性，不要长期设置。"),
    _p("GIT_SSL_NO_VERIFY", CAT_PROXY, "关闭 Git HTTPS 证书校验，仅限临时排错。", "1", note="同样属于降低安全性的临时手段。"),
    _p("SSL_CERT_FILE", CAT_PROXY, "OpenSSL 系工具使用的根证书文件。", r"C:\certs\corp-ca.pem"),
    _p("REQUESTS_CA_BUNDLE", CAT_PROXY, "Python requests 使用的根证书文件。", r"C:\certs\corp-ca.pem"),
    _p("CURL_CA_BUNDLE", CAT_PROXY, "curl 使用的根证书文件。", r"C:\certs\corp-ca.pem"),

    # ------------------------------------------------------------ 构建与编译
    _p("CC", CAT_BUILD, "C 编译器命令，构建脚本据此选择工具链。", "clang"),
    _p("CXX", CAT_BUILD, "C++ 编译器命令。", "clang++"),
    _p("CPP", CAT_BUILD, "C 预处理器命令。", "clang -E"),
    _p("CFLAGS", CAT_BUILD, "C 编译选项，例如优化级别与告警。", "-O2 -Wall"),
    _p("CXXFLAGS", CAT_BUILD, "C++ 编译选项。", "-O2 -Wall -std=c++20"),
    _p("CPPFLAGS", CAT_BUILD, "预处理器选项，主要用于头文件搜索路径。", r"-IC:\tools\include"),
    _p("LDFLAGS", CAT_BUILD, "链接选项，例如库搜索路径。", r"-LC:\tools\lib"),
    _p("LIB", CAT_BUILD, "MSVC 链接器库搜索路径。", r"C:\tools\lib"),
    _p("INCLUDE", CAT_BUILD, "MSVC 头文件搜索路径。", r"C:\tools\include"),
    _p("MAKEFLAGS", CAT_BUILD, "make 默认参数，例如并行任务数。", "-j8"),
    _p("NINJAFLAGS", CAT_BUILD, "Ninja 默认参数。", "-j8"),
    _p("CMAKE_GENERATOR", CAT_BUILD, "默认 CMake 生成器，避免每次 -G 指定。", "Ninja"),
    _p("CMAKE_BUILD_TYPE", CAT_BUILD, "默认 CMake 构建类型。", "RelWithDebInfo"),
    _p("RUSTFLAGS", CAT_BUILD, "Rust 编译选项，例如目标 CPU 特性。", "-C target-cpu=native"),

    # ------------------------------------------------------------ 调试与日志
    _p("RUST_LOG", CAT_DEBUG, "Rust tracing/log 过滤级别。", "info,my_crate=debug"),
    _p("RUST_BACKTRACE", CAT_DEBUG, "Rust panic 时输出完整回溯。", "1"),
    _p("PYTHONUNBUFFERED", CAT_DEBUG, "关闭 Python 输出缓冲，日志可实时看到。", "1"),
    _p("PYTHONWARNINGS", CAT_DEBUG, "Python 警告过滤规则。", "default"),
    _p("PYTHONFAULTHANDLER", CAT_DEBUG, "崩溃时输出 Python 栈回溯。", "1"),
    _p("NODE_DEBUG", CAT_DEBUG, "Node 内置模块调试输出，例如 net,tls。", "net,tls"),
    _p("DEBUG", CAT_DEBUG, "通用调试开关，很多框架读取。", "1"),
    _p("LOG_LEVEL", CAT_DEBUG, "通用日志级别，供应用或脚本读取。", "DEBUG"),
    _p("JAVA_TOOL_OPTIONS", CAT_DEBUG, "JVM 启动参数，对 java 命令全局生效。", "-Xmx2g"),
    _p("_JAVA_OPTIONS", CAT_DEBUG, "JVM 启动参数（优先级更高，常用于临时覆盖）。", "-Dfile.encoding=UTF-8"),

    # ------------------------------------------------------------ 终端与本地化
    _p("LANG", CAT_TERM, "默认语言与编码，Unix 风格工具读取。", "zh_CN.UTF-8"),
    _p("LC_ALL", CAT_TERM, "覆盖所有本地化类别（优先级最高）。", "en_US.UTF-8"),
    _p("LC_CTYPE", CAT_TERM, "字符分类与编码设置。", "zh_CN.UTF-8"),
    _p("TERM", CAT_TERM, "终端类型，影响程序着色与能力探测。", "xterm-256color"),
    _p("COLORTERM", CAT_TERM, "终端色彩能力提示。", "truecolor"),
    _p("EDITOR", CAT_TERM, "默认编辑器，git commit 等命令会调用。", "code --wait"),
    _p("VISUAL", CAT_TERM, "默认可视化编辑器，优先级高于 EDITOR 的场景更多。", "code --wait"),
    _p("PAGER", CAT_TERM, "默认分页器。", "less"),
    _p("LESS", CAT_TERM, "less 的默认行为，例如保留颜色。", "-R"),
    _p("GIT_PAGER", CAT_TERM, "Git 分页器。", "less -R"),

    # ------------------------------------------------------------ 容器与云
    _p("DOCKER_HOST", CAT_CLOUD, "Docker 守护进程地址，远程或 WSL 场景常用。", "tcp://127.0.0.1:2375"),
    _p("DOCKER_BUILDKIT", CAT_CLOUD, "启用 BuildKit 构建后端。", "1"),
    _p("DOCKER_CONFIG", CAT_CLOUD, "Docker CLI 配置目录。", r"%USERPROFILE%\.docker"),
    _p("COMPOSE_PROJECT_NAME", CAT_CLOUD, "docker compose 项目名前缀。", "demo"),
    _p("KUBECONFIG", CAT_CLOUD, "kubectl 配置文件路径，多个用分号分隔。", r"%USERPROFILE%\.kube\config"),
    _p("KUBE_EDITOR", CAT_CLOUD, "kubectl edit 使用的编辑器。", "code --wait"),
    _p("HELM_CACHE_HOME", CAT_CLOUD, "Helm 缓存目录。", r"%LOCALAPPDATA%\helm\cache"),
    _p("AWS_PROFILE", CAT_CLOUD, "AWS CLI 使用的配置档案名。", "default"),
    _p("AWS_REGION", CAT_CLOUD, "AWS SDK 默认区域。", "ap-northeast-1"),
    _p("AWS_DEFAULT_REGION", CAT_CLOUD, "AWS CLI 默认区域（旧名）。", "ap-northeast-1"),
    _p("AWS_ACCESS_KEY_ID", CAT_CLOUD, "AWS 访问密钥 ID。", "AKIAIOSFODNN7EXAMPLE", note="只存放在加密档案中，不要提交到版本库。"),
    _p("AWS_SECRET_ACCESS_KEY", CAT_CLOUD, "AWS 私密访问密钥。", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"),
    _p("AWS_SESSION_TOKEN", CAT_CLOUD, "AWS 临时会话令牌。", "IQoJb3JpZ2luX2VjEXAMPLETOKEN"),
    _p("AZURE_CONFIG_DIR", CAT_CLOUD, "Azure CLI 配置目录。", r"%USERPROFILE%\.azure"),
    _p("GOOGLE_APPLICATION_CREDENTIALS", CAT_CLOUD, "Google 服务账号凭据文件路径。", r"D:\secrets\service-account.json"),
    _p("GITHUB_TOKEN", CAT_CLOUD, "GitHub API/gh 访问令牌。", "ghp_xxxxxxxxxxxxxxxxxxxx"),
    _p("GITLAB_TOKEN", CAT_CLOUD, "GitLab API 访问令牌。", "glpat-xxxxxxxxxxxx"),

    # ------------------------------------------------------------ 数据库
    _p("DATABASE_URL", CAT_DB, "数据库连接串，通常包含用户名与密码。", "postgres://user:pass@localhost:5432/demo"),
    _p("PGHOST", CAT_DB, "PostgreSQL 主机地址。", "localhost"),
    _p("PGPORT", CAT_DB, "PostgreSQL 端口。", "5432"),
    _p("PGUSER", CAT_DB, "PostgreSQL 用户名。", "postgres"),
    _p("PGPASSWORD", CAT_DB, "PostgreSQL 密码。", "your-password"),
    _p("PGDATABASE", CAT_DB, "PostgreSQL 默认数据库名。", "demo"),
    _p("PGSSLMODE", CAT_DB, "PostgreSQL SSL 模式。", "prefer"),
    _p("MYSQL_HOST", CAT_DB, "MySQL 主机地址。", "127.0.0.1"),
    _p("MYSQL_TCP_PORT", CAT_DB, "MySQL 端口。", "3306"),
    _p("MYSQL_PWD", CAT_DB, "MySQL 客户端密码（不建议长期使用明文变量）。", "your-password"),
    _p("MONGO_URI", CAT_DB, "MongoDB 连接串。", "mongodb://localhost:27017/demo"),
    _p("REDIS_URL", CAT_DB, "Redis 连接串。", "redis://localhost:6379/0"),

    # ------------------------------------------------------------ 应用与密钥
    _p("API_KEY", CAT_APP, "第三方 API 访问密钥。", "sk-xxxxxxxxxxxxxxxx"),
    _p("API_TOKEN", CAT_APP, "第三方 API 令牌。", "token-xxxxxxxx"),
    _p("SECRET_KEY", CAT_APP, "应用签名/加密密钥。", "change-me-to-a-random-string"),
    _p("JWT_SECRET", CAT_APP, "JWT 签名密钥。", "change-me-to-a-random-string"),
    _p("ACCESS_TOKEN", CAT_APP, "访问令牌。", "ya29.xxxxxxxx"),
    _p("REFRESH_TOKEN", CAT_APP, "刷新令牌。", "1//xxxxxxxx"),
    _p("SESSION_SECRET", CAT_APP, "会话加密密钥。", "change-me-to-a-random-string"),
    _p("ENCRYPTION_KEY", CAT_APP, "对称加密密钥。", "base64-encoded-key"),
    _p("PRIVATE_KEY", CAT_APP, "私钥内容或私钥文件路径。", r"D:\secrets\private.pem"),
    _p("SIGNING_KEY", CAT_APP, "代码/制品签名密钥。", "change-me-to-a-random-string"),

    # ------------------------------------------------------------ Windows 系统
    _p("COMSPEC", CAT_WIN, "命令解释器路径，由系统设置。", r"C:\Windows\system32\cmd.exe"),
    _p("SYSTEMROOT", CAT_WIN, "Windows 安装目录（也写作 SystemRoot）。", r"C:\Windows"),
    _p("WINDIR", CAT_WIN, "Windows 目录，部分旧程序读取。", r"C:\Windows"),
    _p("NUMBER_OF_PROCESSORS", CAT_WIN, "逻辑处理器数量，构建脚本常用来决定并行度。", "16"),
    _p("PROCESSOR_ARCHITECTURE", CAT_WIN, "处理器架构标识。", "AMD64"),
    _p("USERNAME", CAT_WIN, "当前用户名。", "me"),
    _p("SESSIONNAME", CAT_WIN, "当前会话名（控制台/远程桌面）。", "Console"),
)


def all_presets() -> tuple[EnvPreset, ...]:
    return PRESETS


def count() -> int:
    return len(PRESETS)


def categories() -> tuple[str, ...]:
    return CATEGORIES


def count_by_category() -> dict[str, int]:
    counts: dict[str, int] = {name: 0 for name in CATEGORIES}
    for item in PRESETS:
        counts[item.category] = counts.get(item.category, 0) + 1
    return counts


def get(key: str) -> EnvPreset | None:
    """按变量名查找，不区分大小写；未收录返回 None。"""
    needle = (key or "").strip().lower()
    if not needle:
        return None
    for item in PRESETS:
        if item.key.lower() == needle:
            return item
    return None


def search(category: str | None = None, keyword: str | None = None) -> list[EnvPreset]:
    """按分类与关键词过滤；关键词匹配变量名、分类与说明（不区分大小写）。"""
    needle = (keyword or "").strip().lower()
    results: list[EnvPreset] = []
    for item in PRESETS:
        if category and item.category != category:
            continue
        if needle and (
            needle not in item.key.lower()
            and needle not in item.category.lower()
            and needle not in item.description.lower()
            and needle not in item.example.lower()
        ):
            continue
        results.append(item)
    return results


def validate() -> list[str]:
    """返回目录自身的问题列表；为空表示通过（测试与启动自检使用）。"""
    problems: list[str] = []
    seen: set[str] = set()
    for item in PRESETS:
        if not _KEY_RE.match(item.key):
            problems.append(f"变量名不合法：{item.key}")
        lower = item.key.lower()
        if lower in seen:
            problems.append(f"变量名重复：{item.key}")
        seen.add(lower)
        if item.category not in CATEGORIES:
            problems.append(f"未知分类：{item.key} -> {item.category}")
        if item.sensitive != looks_sensitive_key(item.key):
            problems.append(f"敏感标记不一致：{item.key}")
        if not item.description:
            problems.append(f"缺少说明：{item.key}")
    if len(PRESETS) < 100:
        problems.append(f"常用环境变量数量不足 100：{len(PRESETS)}")
    for name in CATEGORIES:
        if sum(1 for item in PRESETS if item.category == name) == 0:
            problems.append(f"分类为空：{name}")
    return problems
