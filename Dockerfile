FROM python:3.13-alpine AS builder

WORKDIR /build
COPY pyproject.toml .
COPY kiwix_client/ kiwix_client/
COPY kiwix_mcp/ kiwix_mcp/
COPY kiwix_ovos/ kiwix_ovos/

RUN pip install --no-cache-dir --prefix=/install .


FROM python:3.13-alpine

COPY --from=builder /install /usr/local

ENV KIWIX_BASE_URL=""
ENV TRANSPORT="streamable-http"

EXPOSE 8000

ENTRYPOINT ["kiwix-mcp"]
CMD ["--transport", "${TRANSPORT}", "--kiwix-base-url", "${KIWIX_BASE_URL}"]
