#!/bin/bash
# CorbanX API - Instalador automático
# Uso: curl -sO https://raw.githubusercontent.com/rodrigomo-hub/corbanx-api/main/instalar.sh && bash instalar.sh

set -e

REPO="https://github.com/rodrigomo-hub/corbanx-api.git"
INSTALL_DIR="/opt/corbanx-api"
SERVICE_NAME="corbanx-api"
PORT=8004

echo ""
echo "======================================"
echo "  CorbanX API - Instalador"
echo "  Porta: $PORT"
echo "======================================"
echo ""

# Dependências do sistema
echo "[1/5] Instalando dependências do sistema..."
apt-get update -qq
apt-get install -y -qq python3 python3-pip python3-venv git curl

# Clona ou atualiza repositório
echo "[2/5] Clonando repositório..."
if [ -d "$INSTALL_DIR" ]; then
    echo "  → Diretório existe, atualizando..."
    cd "$INSTALL_DIR"
    git pull origin main
else
    git clone "$REPO" "$INSTALL_DIR"
    cd "$INSTALL_DIR"
fi

# Virtualenv + dependências Python
echo "[3/5] Configurando ambiente Python..."
python3 -m venv venv
venv/bin/pip install --upgrade pip -q
venv/bin/pip install -r requirements.txt -q

# Systemd service
echo "[4/5] Configurando serviço systemd..."
cp corbanx-api.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable $SERVICE_NAME
systemctl restart $SERVICE_NAME

# Verificação
echo "[5/5] Verificando serviço..."
sleep 3
STATUS=$(systemctl is-active $SERVICE_NAME)
if [ "$STATUS" = "active" ]; then
    echo ""
    echo "======================================"
    echo "  ✅ CorbanX API instalada com sucesso!"
    echo "  URL: http://$(curl -s ifconfig.me):$PORT"
    echo "  Health: http://$(curl -s ifconfig.me):$PORT/"
    echo "======================================"
else
    echo ""
    echo "❌ Serviço não subiu. Verifique:"
    echo "  journalctl -u $SERVICE_NAME -n 50"
    exit 1
fi
