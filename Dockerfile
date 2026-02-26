FROM python:3.11-slim

# 安装 Camoufox/Firefox 必需的系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgtk-3-0 libdbus-glib-1-2 libxt6 libx11-xcb1 libasound2 \
    libxrender1 libfontconfig1 libfreetype6 xvfb \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 安装 Python 依赖
RUN pip install --no-cache-dir quart camoufox patchright psutil rich

# 下载 Camoufox 浏览器引擎
RUN python -c "from camoufox.sync_api import Camoufox; print('Camoufox ready')" 2>/dev/null || true

# 复制 Solver 代码
COPY *.py /app/

# 暴露端口
EXPOSE 5000

# 启动命令：自适应 Zeabur 分配的端口，降低并发数为 1 防止小鸡爆内存
CMD ["sh", "-c", "Xvfb :99 -screen 0 1024x768x16 & export DISPLAY=:99 && python api_solver.py --browser_type camoufox --thread 1 --headless --port ${PORT:-5000}"]
