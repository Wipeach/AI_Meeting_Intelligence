"""阿里云 OSS 上传工具（线程安全，流式上传大文件）。

配置走环境变量：

    OSS_ACCESS_ID / OSS_ACCESS_SECRET / OSS_ENDPOINT / OSS_BUCKET_NAME / OSS_KEY_PREFIX

oss2 懒加载，未安装时本模块仍可被导入。
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Optional


class OSSUploader:
    """阿里云 OSS 上传器，按需初始化 bucket（线程安全）。"""

    def __init__(
        self,
        *,
        access_id: Optional[str] = None,
        access_secret: Optional[str] = None,
        endpoint: Optional[str] = None,
        bucket_name: Optional[str] = None,
        key_prefix: str = "",
    ):
        self.access_id = access_id or os.getenv("OSS_ACCESS_ID", "")
        self.access_secret = access_secret or os.getenv("OSS_ACCESS_SECRET", "")
        endpoint = endpoint or os.getenv("OSS_ENDPOINT", "")
        self.endpoint = endpoint.replace("https://", "").replace("http://", "")
        self.bucket_name = bucket_name or os.getenv("OSS_BUCKET_NAME", "")
        self.key_prefix = (key_prefix or os.getenv("OSS_KEY_PREFIX", "meeting/")).rstrip("/") + "/"
        self._bucket = None
        self._lock = threading.Lock()

    @property
    def bucket(self):
        if self._bucket is None:
            with self._lock:
                if self._bucket is None:
                    try:
                        import oss2
                    except ImportError as exc:  # pragma: no cover
                        raise RuntimeError("缺少依赖 oss2，请先 pip install oss2") from exc
                    if not (self.access_id and self.access_secret and self.endpoint and self.bucket_name):
                        raise RuntimeError("OSS 配置不完整，请检查 OSS_ACCESS_ID / SECRET / ENDPOINT / BUCKET_NAME")
                    auth = oss2.Auth(self.access_id, self.access_secret)
                    self._bucket = oss2.Bucket(auth, f"https://{self.endpoint}", self.bucket_name)
        return self._bucket

    def upload_bytes(self, content: bytes, object_key: str) -> str:
        """上传字节数据（小文件，如纪要文本），返回公开 URL。"""
        result = self.bucket.put_object(object_key, content)
        if result.status != 200:
            raise RuntimeError(f"OSS 上传失败：status={result.status}")
        return self._public_url(object_key)

    def upload_file(self, file_path: str, object_key: Optional[str] = None) -> str:
        """上传本地文件（流式，避免大视频读进内存）。"""
        path = Path(file_path)
        object_key = object_key or f"{self.key_prefix}{path.name}"
        with path.open("rb") as handle:
            result = self.bucket.put_object(object_key, handle)
        if result.status != 200:
            raise RuntimeError(f"OSS 上传失败：status={result.status}")
        return self._public_url(object_key)

    def upload_stream(self, data, object_key: str, chunk_size: int = 5 * 1024 * 1024) -> str:
        """流式直传：从 file-like（如下载响应的 raw 流）分块读出，用【分片上传】逐块传 OSS。

        - 不落本地磁盘：边从源流读、边上传分片。
        - 无需预先知道总长度，也无需源可 seek（分片各自带长度）。
        - 下载(入站)与上传(出站)是两条不抢带宽的管道，能并行，省一次磁盘读写。
        - 失败时自动中止分片，不留半截对象。

        put_object 走不通非可定位流（OSS 不支持 chunked PUT），故用 multipart。
        """
        import oss2
        upload_id = self.bucket.init_multipart_upload(object_key).upload_id
        parts: list = []
        part_number = 1
        try:
            while True:
                chunk = data.read(chunk_size)
                if not chunk:
                    break
                result = self.bucket.upload_part(object_key, upload_id, part_number, chunk)
                parts.append(oss2.models.PartInfo(part_number, result.etag))
                part_number += 1
            if not parts:  # 空流：放一个空对象
                self.bucket.put_object(object_key, b"")
            else:
                self.bucket.complete_multipart_upload(object_key, upload_id, parts)
        except Exception:
            try:
                self.bucket.abort_multipart_upload(object_key, upload_id)
            except Exception:  # noqa: BLE001
                pass
            raise
        return self._public_url(object_key)

    def prefixed_key(self, name: str) -> str:
        return f"{self.key_prefix}{name}"

    def _public_url(self, object_key: str) -> str:
        """标准 OSS 公开 URL；私有 bucket 需改用 sign_url。"""
        return f"https://{self.bucket_name}.{self.endpoint}/{object_key}"
