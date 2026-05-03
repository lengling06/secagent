#!/usr/bin/env bash
# ============================================================================
# SecAgent installer
# ============================================================================
# Usage (after pushing to GitHub):
#   curl -fsSL https://raw.githubusercontent.com/<you>/secagent/main/install.sh | bash
#
# Local mode (when run from inside a checkout):
#   bash install.sh
#
# Env vars / flags:
#   SECAGENT_REPO=<git url>     pip-install from this URL instead of default
#   SECAGENT_BRANCH=<branch>    git branch (default: main)
#   SKIP_NODE=1                 do not auto-install Node (you handle it)
#   SKIP_DOCKER=1               do not auto-install Docker (you handle it)
#   SKIP_RECON=1                do not install nmap/dnsutils
#   --no-confirm                non-interactive (yes to everything)
#   --help                      this message
# ============================================================================

set -euo pipefail

# ---------- config ----------
REPO_DEFAULT="https://github.com/CHANGE-ME/secagent.git"   # ←—— 改成你的仓库 URL
BRANCH_DEFAULT="main"

REPO="${SECAGENT_REPO:-$REPO_DEFAULT}"
BRANCH="${SECAGENT_BRANCH:-$BRANCH_DEFAULT}"

NO_CONFIRM=0
for arg in "$@"; do
    case "$arg" in
        --no-confirm) NO_CONFIRM=1 ;;
        -h|--help)
            sed -n '2,20p' "$0" | sed 's/^# \?//'
            exit 0
            ;;
    esac
done

# ---------- pretty ----------
if [[ -t 1 ]]; then
    GRN=$'\033[0;32m'; YEL=$'\033[0;33m'; RED=$'\033[0;31m'; CYA=$'\033[0;36m'; OFF=$'\033[0m'
else
    GRN=""; YEL=""; RED=""; CYA=""; OFF=""
fi
log()  { echo "${GRN}==>${OFF} $*"; }
info() { echo "${CYA}   ${OFF} $*"; }
warn() { echo "${YEL}[warn]${OFF} $*" >&2; }
die()  { echo "${RED}[err]${OFF} $*" >&2; exit 1; }

ask_yn() {
    # ask_yn "prompt" default(Y|N)
    local prompt="$1" default="${2:-Y}"
    if [[ "$NO_CONFIRM" == "1" ]]; then
        [[ "$default" == "Y" ]] && return 0 || return 1
    fi
    if [[ ! -t 0 ]]; then
        # piped stdin (curl|bash); fall back to default
        [[ "$default" == "Y" ]] && return 0 || return 1
    fi
    local hint="[Y/n]"; [[ "$default" == "N" ]] && hint="[y/N]"
    local ans
    read -rp "$prompt $hint: " ans
    ans="${ans:-$default}"
    [[ "$ans" =~ ^[Yy]$ ]]
}

# ---------- detect OS ----------
detect_os() {
    if [[ "$(uname -s)" == "Darwin" ]]; then echo "macos"; return; fi
    if [[ -f /etc/os-release ]]; then
        # shellcheck disable=SC1091
        . /etc/os-release
        echo "${ID:-unknown}"
        return
    fi
    echo "unknown"
}
OS=$(detect_os)

PKG=""
case "$OS" in
    ubuntu|debian|kali|raspbian) PKG="apt" ;;
    fedora|rhel|centos|rocky|almalinux) PKG="dnf" ;;
    arch|manjaro)               PKG="pacman" ;;
    macos)                      PKG="brew" ;;
    *) PKG="unknown" ;;
esac

log "detected OS: $OS  (package manager: $PKG)"

# ---------- helpers ----------
have() { command -v "$1" >/dev/null 2>&1; }

apt_install() { sudo apt-get update -qq && sudo apt-get install -y "$@"; }
dnf_install() { sudo dnf install -y "$@"; }
pacman_install() { sudo pacman -Sy --noconfirm "$@"; }
brew_install() { brew install "$@"; }

pkg_install() {
    case "$PKG" in
        apt)    apt_install "$@" ;;
        dnf)    dnf_install "$@" ;;
        pacman) pacman_install "$@" ;;
        brew)   brew_install "$@" ;;
        *) die "don't know how to install on $OS — install these manually: $*" ;;
    esac
}

# ---------- 1. python ----------
ensure_python() {
    local needed_major=3 needed_minor=10
    if have python3; then
        local v; v=$(python3 -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')
        local maj=${v%.*} min=${v#*.}
        if [[ $maj -ge $needed_major && $min -ge $needed_minor ]]; then
            info "python: $v (ok)"
            return
        fi
        warn "python $v found but need >= $needed_major.$needed_minor"
    fi
    log "installing python3 + venv + pip..."
    case "$PKG" in
        apt)    apt_install python3 python3-venv python3-pip ;;
        dnf)    dnf_install python3 python3-pip ;;
        pacman) pacman_install python python-pip ;;
        brew)   brew_install python@3.11 ;;
        *) die "install python 3.10+ manually" ;;
    esac
}

# ---------- 2. pipx ----------
ensure_pipx() {
    if have pipx; then
        info "pipx: $(pipx --version 2>/dev/null) (ok)"
        return
    fi
    log "installing pipx..."
    case "$PKG" in
        apt)    apt_install pipx || python3 -m pip install --user pipx ;;
        dnf)    dnf_install pipx || python3 -m pip install --user pipx ;;
        pacman) pacman_install python-pipx ;;
        brew)   brew_install pipx ;;
        *)      python3 -m pip install --user pipx ;;
    esac
    # ensure ~/.local/bin on PATH
    python3 -m pipx ensurepath 2>/dev/null || true
    # make pipx visible in this shell session
    export PATH="$HOME/.local/bin:$PATH"
    have pipx || die "pipx install failed; install manually then re-run"
}

# ---------- 3. node ----------
ensure_node() {
    if [[ "${SKIP_NODE:-0}" == "1" ]]; then
        warn "SKIP_NODE=1 — js_execute and MCP servers will not work without Node 20+"
        return
    fi
    if have node; then
        local v; v=$(node -v | sed 's/v//')
        local maj=${v%%.*}
        if [[ $maj -ge 20 ]]; then
            info "node: v$v (ok)"
            return
        fi
        warn "node v$v found but recommend >= 20"
    fi
    log "installing node 20..."
    case "$PKG" in
        apt)
            curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
            apt_install nodejs
            ;;
        dnf)
            curl -fsSL https://rpm.nodesource.com/setup_20.x | sudo bash -
            dnf_install nodejs
            ;;
        pacman) pacman_install nodejs npm ;;
        brew)   brew_install node@20 ;;
        *) warn "skip node; install Node 20+ manually for js_execute / MCP" ;;
    esac
}

# ---------- 4. docker ----------
ensure_docker() {
    if [[ "${SKIP_DOCKER:-0}" == "1" ]]; then
        warn "SKIP_DOCKER=1 — js_execute will fall back to node-permission (network NOT blocked)"
        return
    fi
    if have docker && docker info >/dev/null 2>&1; then
        info "docker: ready"
    else
        if ! ask_yn "install docker for js_execute network sandbox?" Y; then
            warn "skipping docker; js_execute will use node-permission fallback (no network isolation)"
            return
        fi
        log "installing docker..."
        case "$PKG" in
            apt|dnf)
                curl -fsSL https://get.docker.com | sh
                sudo usermod -aG docker "$USER" || true
                warn "log out and back in (or run 'newgrp docker') for docker group to take effect"
                ;;
            pacman) pacman_install docker
                    sudo systemctl enable --now docker
                    sudo usermod -aG docker "$USER" || true ;;
            brew)   warn "install Docker Desktop manually: https://www.docker.com/products/docker-desktop" ;;
            *)      warn "install docker manually" ;;
        esac
    fi
    # pre-pull sandbox image if docker is usable
    if docker info >/dev/null 2>&1; then
        log "pre-pulling node:20-alpine for js_execute sandbox..."
        docker pull node:20-alpine >/dev/null 2>&1 || \
            warn "could not pre-pull node:20-alpine (will pull on first js_execute)"
    fi
}

# ---------- 5. recon CLIs (optional) ----------
ensure_recon() {
    if [[ "${SKIP_RECON:-0}" == "1" ]]; then return; fi
    if have nmap && have dig; then
        info "nmap + dig: ok"
    else
        log "installing nmap + dig..."
        case "$PKG" in
            apt)    apt_install nmap dnsutils ;;
            dnf)    dnf_install nmap bind-utils ;;
            pacman) pacman_install nmap bind-tools ;;
            brew)   brew_install nmap bind ;;
        esac
    fi
    # subfinder/httpx/dnsx via go install — only if Go is installed already
    if have go; then
        if ! have subfinder; then
            log "installing ProjectDiscovery toolchain..."
            GO111MODULE=on go install -v github.com/projectdiscovery/subfinder/v2/cmd/subfinder@latest
            GO111MODULE=on go install -v github.com/projectdiscovery/httpx/cmd/httpx@latest
            GO111MODULE=on go install -v github.com/projectdiscovery/dnsx/cmd/dnsx@latest
            warn "make sure \$HOME/go/bin is on your PATH"
        fi
    else
        info "(skip subfinder/httpx/dnsx — install Go and re-run if you want recon tools)"
    fi
}

# ---------- 6. install secagent ----------
install_secagent() {
    local source=""
    # local mode? (script lives next to a secagent pyproject.toml)
    local script_dir; script_dir=$(cd "$(dirname "$0")" 2>/dev/null && pwd) || script_dir=""
    if [[ -n "$script_dir" && -f "$script_dir/pyproject.toml" ]] && grep -q '^name = "secagent"' "$script_dir/pyproject.toml"; then
        source="$script_dir"
        log "installing secagent from local checkout: $source"
    else
        if [[ "$REPO" == *"CHANGE-ME"* ]]; then
            die "install.sh has a placeholder repo URL. Either:
   - edit REPO_DEFAULT in install.sh to your real GitHub URL, OR
   - run with: SECAGENT_REPO=https://github.com/<you>/secagent.git bash install.sh"
        fi
        source="git+$REPO@$BRANCH"
        log "installing secagent from $source"
    fi
    pipx install --force "$source"
}

# ---------- 7. post-install ----------
post_install() {
    log ""
    log "${GRN}done.${OFF}"
    info ""
    info "next steps:"
    info "  1. open a new shell (or run: source ~/.bashrc)  ← so pipx PATH activates"
    info "  2. secagent init       # configure LLM (one-time)"
    info "  3. secagent target https://example.com/        # try it"
    info ""
    info "if 'secagent: command not found' after step 1:"
    info "  echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.bashrc"
    info "  source ~/.bashrc"
    info ""
    if have docker && ! docker info >/dev/null 2>&1; then
        warn "docker is installed but you cannot use it without re-login. Run: newgrp docker"
    fi
}

# ---------- main ----------
main() {
    log "SecAgent installer"
    info "repo:   $REPO"
    info "branch: $BRANCH"
    info ""

    if [[ "$EUID" -eq 0 ]]; then
        warn "running as root; pipx will install for root user only"
        if ! ask_yn "continue as root?" N; then
            die "abort"
        fi
    fi

    ensure_python
    ensure_pipx
    ensure_node
    ensure_docker
    ensure_recon
    install_secagent
    post_install
}

main "$@"
