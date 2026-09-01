"""Хранилище поверх S3-совместимого сервиса.

Используется синхронный boto3, вызовы уводятся в пул потоков. Асинхронные
обёртки вроде aioboto3 жёстко привязаны к версиям botocore и регулярно
ломают сборку, а логотипы читаются редко и кэшируются — выигрыш от
настоящей асинхронности здесь нулевой.
"""

from typing import Any

import boto3
from anyio import to_thread
from botocore.client import Config
from botocore.exceptions import ClientError

from nosbp.core.config import Settings
from nosbp.storage.base import Storage


class S3Storage(Storage):
    """Реализация :class:`Storage` поверх S3."""

    def __init__(self, settings: Settings) -> None:
        self._bucket = settings.s3_bucket
        self._client: Any = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint_url,
            region_name=settings.s3_region,
            aws_access_key_id=settings.s3_access_key,
            aws_secret_access_key=settings.s3_secret_key,
            config=Config(signature_version="s3v4", retries={"max_attempts": 3}),
        )

    async def put(self, key: str, data: bytes, *, content_type: str) -> None:
        await to_thread.run_sync(self._put_sync, key, data, content_type)

    async def get(self, key: str) -> bytes | None:
        return await to_thread.run_sync(self._get_sync, key)

    async def delete(self, key: str) -> None:
        await to_thread.run_sync(self._delete_sync, key)

    # ------------------------------------------------------------------
    # Синхронные операции, которые уходят в пул потоков
    # ------------------------------------------------------------------

    def _put_sync(self, key: str, data: bytes, content_type: str) -> None:
        self._client.put_object(
            Bucket=self._bucket, Key=key, Body=data, ContentType=content_type
        )

    def _get_sync(self, key: str) -> bytes | None:
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=key)
        except ClientError as exc:
            # Отсутствующий файл — это не ошибка, а штатная ситуация:
            # у организации может просто не быть логотипа.
            if exc.response.get("Error", {}).get("Code") in {"NoSuchKey", "404"}:
                return None
            raise
        body: bytes = response["Body"].read()
        return body

    def _delete_sync(self, key: str) -> None:
        self._client.delete_object(Bucket=self._bucket, Key=key)

    def ensure_bucket(self) -> None:
        """Создаёт бакет, если его ещё нет.

        Нужно при первом запуске против локального MinIO. На боевом S3
        бакет обычно создан заранее, и вызов просто ничего не делает.
        """
        try:
            self._client.head_bucket(Bucket=self._bucket)
        except ClientError:
            self._client.create_bucket(Bucket=self._bucket)
