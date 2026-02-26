"""
浏览器指纹配置

为 Camoufox/Playwright 浏览器提供随机化的 User-Agent 和 Sec-CH-UA。
"""
import random


class browser_config:

    @staticmethod
    def get_random_browser_config(browser_type):
        """返回随机浏览器配置: (浏览器名, 版本, User-Agent, Sec-CH-UA)"""
        versions = ["120.0.0.0", "121.0.0.0", "122.0.0.0", "124.0.0.0", "125.0.0.0"]
        ver = random.choice(versions)
        major = ver.split(".")[0]
        ua = (
            f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            f"AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{ver} Safari/537.36"
        )
        sec_ch_ua = f'"Not(A:Brand";v="99", "Google Chrome";v="{major}", "Chromium";v="{major}"'
        return "chrome", ver, ua, sec_ch_ua

    @staticmethod
    def get_browser_config(name, version):
        """返回指定版本的浏览器配置: (User-Agent, Sec-CH-UA)"""
        ua = (
            f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            f"AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{version} Safari/537.36"
        )
        sec_ch_ua = f'"Google Chrome";v="{version}", "Chromium";v="{version}"'
        return ua, sec_ch_ua
