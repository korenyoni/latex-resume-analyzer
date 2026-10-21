FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# tectonic: self-contained LaTeX compiler (musl static build).
ARG TARGETARCH
RUN python - <<EOF
import os, tarfile, tempfile, urllib.request
arch = {"amd64": "x86_64", "arm64": "aarch64"}.get(os.environ["TARGETARCH"], "x86_64")
url = f"https://github.com/tectonic-typesetting/tectonic/releases/download/tectonic%400.15.0/tectonic-0.15.0-{arch}-unknown-linux-musl.tar.gz"
with tempfile.TemporaryFile() as f:
    f.write(urllib.request.urlopen(url).read()); f.seek(0)
    tarfile.open(fileobj=f).extractall("/usr/local/bin")
EOF

# Warm the LaTeX package cache so compiles work offline at runtime.
RUN printf '\\documentclass{article}\\usepackage{enumitem}\\usepackage{titlesec}\
\\usepackage{geometry}\\usepackage{hyperref}\\usepackage{fontawesome5}\
\\begin{document}\\section{A}\\begin{itemize}\\item b\\end{itemize}\\end{document}' \
    > /tmp/warm.tex \
    && tectonic /tmp/warm.tex && rm /tmp/warm.tex

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# The sample resume is baked in; app startup copies it into uploads/ when empty.
EXPOSE 8000
CMD ["uvicorn", "app.web:app", "--host", "0.0.0.0", "--port", "8000"]
