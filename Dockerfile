FROM python:3.11-slim

# 安装 Camoufox/Firefox 必需的系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgtk-3-0 libdbus-glib-1-2 libxt6 libx11-xcb1 libasound2 \
    libxrender1 libfontconfig1 libfreetype6 xvfb \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 环境变量：让 Python stdout 直接输出不缓存；配置默认系统参数
ENV PYTHONUNBUFFERED=1
ENV SOLVER_THREADS=1
ENV SOLVER_HEADLESS=true
ENV SOLVER_BROWSER=camoufox

# 安装 Python 依赖
RUN pip install --no-cache-dir quart camoufox patchright psutil rich

# 下载 Camoufox 浏览器引擎（必须提前下载，否则服务启动耗时过长会导致 502）
RUN python -m camoufox fetch

# 复制 Solver 代码
COPY *.py /app/

# 暴露端口
EXPOSE 5000

# 启动命令：自适应系统分配的端口（使用环境变量），并默认开启 Xvfb
CMD ["sh", "-c", "Xvfb :99 -screen 0 1024x768x16 & export DISPLAY=:99 && python api_solver.py"]
