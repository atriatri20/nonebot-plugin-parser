import re
import asyncio
from io import BytesIO
from typing import ClassVar
from PIL import Image
import msgspec
from httpx import AsyncClient
from nonebot import logger
import tempfile
from pathlib import Path

# 调整导入路径
from ..base import (
    COMMON_TIMEOUT,
    Platform,
    BaseParser,
    PlatformEnum,
    ParseException,
    handle,
)
from ...download import DOWNLOADER  # 三个点，从nonebot_plugin_parser导入
from ..data import ImageContent  # 两个点，从parsers导入


class DouyinParser(BaseParser):
    # 平台信息
    platform: ClassVar[Platform] = Platform(name=PlatformEnum.DOUYIN, display_name="抖音")

    # https://v.douyin.com/_2ljF4AmKL8
    @handle("v.douyin", r"v\.douyin\.com/[a-zA-Z0-9_\-]+")
    @handle("jx.douyin", r"jx\.douyin\.com/[a-zA-Z0-9_\-]+")
    async def _parse_short_link(self, searched: re.Match[str]):
        """解析短链接"""
        url = f"https://{searched.group(0)}"
        return await self.parse_with_redirect(url)

    # https://www.douyin.com/video/7521023890996514083
    # https://www.douyin.com/note/7469411074119322899
    @handle("douyin", r"douyin\.com/(?P<ty>video|note)/(?P<vid>\d+)")
    @handle("iesdouyin", r"iesdouyin\.com/share/(?P<ty>slides|video|note)/(?P<vid>\d+)")
    @handle("m.douyin", r"m\.douyin\.com/share/(?P<ty>slides|video|note)/(?P<vid>\d+)")
    # https://jingxuan.douyin.com/m/video/7574300896016862490?app=yumme&utm_source=copy_link
    @handle(
        "jingxuan.douyin",
        r"jingxuan\.douyin.com/m/(?P<ty>slides|video|note)/(?P<vid>\d+)",
    )
    async def _parse_douyin(self, searched: re.Match[str]):
        """解析抖音主链接"""
        ty, vid = searched.group("ty"), searched.group("vid")
        if ty == "slides":
            return await self.parse_slides(vid)

        for url in (
            self._build_m_douyin_url(ty, vid),
            self._build_iesdouyin_url(ty, vid),
        ):
            try:
                return await self.parse_video(url)
            except ParseException as e:
                logger.warning(f"failed to parse {url}, error: {e}")
                continue
        raise ParseException("分享已删除或资源直链提取失败, 请稍后再试")

    @staticmethod
    def _build_iesdouyin_url(ty: str, vid: str) -> str:
        """构建iesdouyin域名的URL"""
        return f"https://www.iesdouyin.com/share/{ty}/{vid}"

    @staticmethod
    def _build_m_douyin_url(ty: str, vid: str) -> str:
        """构建m.douyin域名的URL"""
        return f"https://m.douyin.com/share/{ty}/{vid}"

    async def _convert_webp_to_jpg(self, url: str, index: int = 0) -> tuple[int, bytes | None]:
        """异步下载WebP并转换为JPG字节数据

        Args:
            url: WebP图片URL
            index: 图片索引，用于日志追踪

        Returns:
            (索引, JPG字节数据) 或 (索引, None) 如果转换失败
        """
        try:
            async with AsyncClient(headers=self.android_headers, verify=False) as client:
                response = await client.get(url)
                response.raise_for_status()

                # 打开WebP图像
                webp_image = Image.open(BytesIO(response.content))

                # 确保转换为RGB模式（JPG不支持透明度）
                if webp_image.mode != "RGB":
                    webp_image = webp_image.convert("RGB")

                # 转换为JPG字节数据
                output = BytesIO()
                webp_image.save(output, format="JPEG", quality=95)
                jpg_data = output.getvalue()

                logger.debug(f"[抖音转换] 图片{index}: {url[:60]}... {len(jpg_data)} bytes")
                return index, jpg_data

        except Exception as e:
            logger.warning(f"[抖音转换] 失败 图片{index}: {url[:60]}... {e}")
            return index, None

    async def _create_jpg_task(self, jpg_data: bytes) -> Path:
        """将JPG字节数据保存到临时文件并返回路径

        用于创建兼容ImageContent的下载任务
        """
        # 创建临时JPG文件
        with tempfile.NamedTemporaryFile(suffix='.jpg', delete=False) as f:
            f.write(jpg_data)
            temp_path = Path(f.name)

        # 记录临时文件路径，用于后续清理（可选）
        if not hasattr(self, '_temp_files'):
            self._temp_files = []
        self._temp_files.append(temp_path)

        logger.debug(f"[临时文件] 创建JPG文件: {temp_path}")
        return temp_path

    def create_image_contents(self, image_sources: list) -> list:
        """创建图片内容，支持URL或JPG字节数据"""
        contents = []
        for source in image_sources:
            if isinstance(source, bytes):
                # 为JPG字节数据创建异步任务
                task = asyncio.create_task(self._create_jpg_task(source))
                contents.append(ImageContent(task))
            elif isinstance(source, str):
                # 为URL使用原有下载器
                task = DOWNLOADER.download_img(source, ext_headers=self.headers)
                contents.append(ImageContent(task))
        return contents

    async def parse_video(self, url: str):
        """解析视频/图集页面"""
        async with AsyncClient(
            headers=self.ios_headers,
            timeout=COMMON_TIMEOUT,
            follow_redirects=False,
            verify=False,
        ) as client:
            response = await client.get(url)
            if response.status_code != 200:
                raise ParseException(f"status: {response.status_code}")
            text = response.text

        pattern = re.compile(
            pattern=r"window\._ROUTER_DATA\s*=\s*(.*?)</script>",
            flags=re.DOTALL,
        )
        matched = pattern.search(text)

        if not matched or not matched.group(1):
            raise ParseException("can't find _ROUTER_DATA in html")

        from .video import RouterData

        video_data = msgspec.json.decode(matched.group(1).strip(), type=RouterData).video_data
        contents = []

        # 处理图片内容（转换为JPG）
        if image_urls := video_data.image_urls:
            logger.info(f"[抖音解析] 发现{len(image_urls)}张图片，开始转换...")

            # 并发转换所有图片，带索引以便追踪
            convert_tasks = [
                self._convert_webp_to_jpg(img_url, i)
                for i, img_url in enumerate(image_urls)
            ]
            results = await asyncio.gather(*convert_tasks)

            # 按索引排序并构建图片源列表
            results.sort(key=lambda x: x[0])  # 按索引排序
            image_sources = []
            success_count = 0

            for idx, jpg_data in results:
                if jpg_data:
                    image_sources.append(jpg_data)
                    success_count += 1
                else:
                    # 转换失败，回退到原始URL
                    image_sources.append(image_urls[idx])

            logger.info(f"[抖音解析] 转换成功: {success_count}/{len(image_urls)}")
            contents.extend(self.create_image_contents(image_sources))

        # 处理视频内容（保持不变）
        elif video_url := video_data.video_url:
            cover_url = video_data.cover_url
            duration = video_data.video.duration if video_data.video else 0
            contents.append(self.create_video_content(video_url, cover_url, duration))

        # 构建作者
        author = self.create_author(video_data.author.nickname, video_data.avatar_url)

        return self.result(
            title=video_data.desc,
            author=author,
            contents=contents,
            timestamp=video_data.create_time,
        )

    async def parse_slides(self, video_id: str):
        """解析图集详情"""
        url = "https://www.iesdouyin.com/web/api/v2/aweme/slidesinfo/"
        params = {
            "aweme_ids": f"[{video_id}]",
            "request_source": "200",
        }
        async with AsyncClient(headers=self.android_headers, verify=False) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()

        from .slides import SlidesInfo

        slides_data = msgspec.json.decode(response.content, type=SlidesInfo).aweme_details[0]
        contents = []

        # 处理图片内容（转换为JPG）
        if image_urls := slides_data.image_urls:
            logger.info(f"[图集解析] 发现{len(image_urls)}张图片，开始转换...")

            convert_tasks = [
                self._convert_webp_to_jpg(img_url, i)
                for i, img_url in enumerate(image_urls)
            ]
            results = await asyncio.gather(*convert_tasks)
            results.sort(key=lambda x: x[0])

            image_sources = []
            success_count = 0

            for idx, jpg_data in results:
                if jpg_data:
                    image_sources.append(jpg_data)
                    success_count += 1
                else:
                    image_sources.append(image_urls[idx])

            logger.info(f"[图集解析] 转换成功: {success_count}/{len(image_urls)}")
            contents.extend(self.create_image_contents(image_sources))

        # 处理动态内容（保持不变）
        if dynamic_urls := slides_data.dynamic_urls:
            contents.extend(self.create_dynamic_contents(dynamic_urls))

        # 构建作者
        author = self.create_author(slides_data.name, slides_data.avatar_url)

        return self.result(
            title=slides_data.desc,
            author=author,
            contents=contents,
            timestamp=slides_data.create_time,
        )
