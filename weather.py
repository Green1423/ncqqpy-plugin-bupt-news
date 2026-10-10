"""Open-Meteo 天气客户端 + WMO 天气码→中文映射（免 API key）。

天气是 best-effort：任何失败都返回 None，由调用方降级，绝不把异常抛进推送主链路。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import aiohttp

API = "https://api.open-meteo.com/v1/forecast"

# WMO Weather interpretation codes (WW) —— Open-Meteo 文档定义的全集
WMO_ZH = {
    0: "晴", 1: "晴间多云", 2: "多云", 3: "阴",
    45: "雾", 48: "雾凇",
    51: "小毛毛雨", 53: "毛毛雨", 55: "大毛毛雨",
    56: "冻毛毛雨", 57: "强冻毛毛雨",
    61: "小雨", 63: "中雨", 65: "大雨",
    66: "冻雨", 67: "强冻雨",
    71: "小雪", 73: "中雪", 75: "大雪", 77: "雪粒",
    80: "小阵雨", 81: "阵雨", 82: "强阵雨",
    85: "小阵雪", 86: "大阵雪",
    95: "雷阵雨", 96: "雷阵雨伴冰雹", 99: "雷阵雨伴强冰雹",
}

# 降水类天气码（需要带伞）：毛毛雨 / 雨 / 阵雨 / 冻雨 / 雷雨冰雹 / 雪
RAIN_CODES = {51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82, 95, 96, 99}
SNOW_CODES = {71, 73, 75, 77, 85, 86}
UMBRELLA_CODES = RAIN_CODES | SNOW_CODES


def describe(code: Optional[int]) -> str:
    """WMO 码 → 中文；未知码回退「未知天气」。"""
    if code is None:
        return "未知天气"
    try:
        return WMO_ZH.get(int(code), "未知天气")
    except (TypeError, ValueError):
        return "未知天气"


@dataclass
class Weather:
    """当天天气（结构化，便于判断是否需要带伞）。"""

    code: Optional[int] = None
    tmin: Optional[float] = None
    tmax: Optional[float] = None
    pop: Optional[int] = None            # 最大降水概率 %
    text: str = ""                       # 渲染后的一行描述
    umbrella: bool = False               # 是否需要提醒带伞


def needs_umbrella(code: Optional[int], pop: Optional[int] = None,
                   pop_threshold: int = 50) -> bool:
    """是否需要提醒带伞。

    判据（满足其一即提醒）：
      1. 天气码本身属于降水类（雨 / 阵雨 / 雷雨 / 冻雨 / 冰雹 / 雪）
      2. 降水概率 >= pop_threshold（默认 50%）——
         覆盖「主导天气码看不出下雨、但大概率有雨」的情况

    注意：`weather_code` 是当天的**主导**天气，单看它可能漏掉「白天阴、傍晚转雨」，
    所以叠加降水概率一起判断。
    """
    try:
        if code is not None and int(code) in UMBRELLA_CODES:
            return True
    except (TypeError, ValueError):
        pass
    try:
        if pop is not None and int(pop) >= int(pop_threshold):
            return True
    except (TypeError, ValueError):
        pass
    return False


def _render(code, tmin, tmax, pop) -> str:
    parts = [describe(code)]
    if tmin is not None and tmax is not None:
        try:
            parts.append(f"{round(tmin)}~{round(tmax)}℃")
        except (TypeError, ValueError):
            pass
    if pop is not None:
        try:
            parts.append(f"降水{round(pop)}%")
        except (TypeError, ValueError):
            pass
    return " ".join(parts)


class WeatherClient:
    """取当天天气。失败一律返回 None。"""

    def __init__(self, label: str = "北京", latitude: float = 39.9608,
                 longitude: float = 116.3590, timeout: int = 20,
                 umbrella_pop: int = 50, logger=None) -> None:
        self.label = label
        self.latitude = latitude
        self.longitude = longitude
        self.timeout = timeout
        self.umbrella_pop = int(umbrella_pop)
        self.logger = logger
        self._session: Optional[aiohttp.ClientSession] = None

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout, connect=8))
        return self._session

    async def today(self) -> Optional[Weather]:
        """返回当天天气；失败返回 None。"""
        params = {
            "latitude": self.latitude,
            "longitude": self.longitude,
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,"
                     "precipitation_probability_max",
            "timezone": "Asia/Shanghai",
            "forecast_days": 1,
        }
        try:
            session = await self._ensure_session()
            async with session.get(API, params=params) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"HTTP {resp.status}")
                data = await resp.json()
            daily = data.get("daily") or {}
            code = (daily.get("weather_code") or [None])[0]
            tmax = (daily.get("temperature_2m_max") or [None])[0]
            tmin = (daily.get("temperature_2m_min") or [None])[0]
            pop = (daily.get("precipitation_probability_max") or [None])[0]
            try:
                code = None if code is None else int(code)
            except (TypeError, ValueError):
                code = None
            try:
                pop = None if pop is None else int(round(pop))
            except (TypeError, ValueError):
                pop = None
            return Weather(
                code=code, tmin=tmin, tmax=tmax, pop=pop,
                text=_render(code, tmin, tmax, pop),
                umbrella=needs_umbrella(code, pop, self.umbrella_pop),
            )
        except Exception as e:  # noqa: BLE001 - 天气是 best-effort
            if self.logger:
                self.logger.warning("[bupt_news] 天气获取失败：%s", e)
            return None

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None


def weather_line(w: Optional[Weather], label: str = "北京",
                 now: Optional[datetime] = None) -> str:
    """组装早报首行。

    - `w` 为 None → 降级为「天气暂不可用」（不带带伞提醒）
    - 需要带伞时追加「 ☂️ 记得带伞」
    """
    now = now or datetime.now()
    body = w.text if w is not None else "天气暂不可用"
    line = f"☀️ {now:%m-%d} {label} {body}"
    if w is not None and w.umbrella:
        line += " ☂️ 记得带伞"
    return line

