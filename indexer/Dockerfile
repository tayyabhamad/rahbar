# rahbar-indexer: the only writer of the Chroma collection (a Job, so it exits).
#
# The build context is the REPOSITORY ROOT, because the image also carries corpus/fetch.py
# and corpus/sources.yaml, which the Job's `fetch` initContainer runs to download the
# documents into an emptyDir (docs/ARCHITECTURE.md, "How the corpus reaches the Job"):
#   docker build -f indexer/Dockerfile -t rahbar-indexer:dev .
# The root .dockerignore keeps corpus/raw, api/, ui/, docs/ ... out of the context.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# OCR for scanned pages: pytesseract needs the tesseract binary and English data,
# pdf2image needs poppler (pdftoppm).
RUN apt-get update \
 && apt-get upgrade -y \
 && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng poppler-utils \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY indexer/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY indexer/pipeline/ ./pipeline/
# The fetcher and the document list, copied by the initContainer into /corpus at run time.
COPY corpus/fetch.py corpus/sources.yaml ./corpus-src/

RUN groupadd --gid 10001 app \
 && useradd --uid 10001 --gid 10001 --create-home --home-dir /home/app --shell /usr/sbin/nologin app \
 && chown -R 10001:10001 /app
USER 10001

CMD ["python", "-m", "pipeline.run"]
