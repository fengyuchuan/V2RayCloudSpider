"""
- 核心功能
    - 订阅池清洗维护
    - 识别不可用链接并剔除
"""
import base64
import warnings
from datetime import datetime
from typing import List
from urllib.parse import urlparse

import requests
from redis.exceptions import ResponseError, ConnectionError

from src.BusinessCentralLayer.middleware.redis_io import RedisClient
from src.BusinessCentralLayer.setting import REDIS_SECRET_KEY, CRAWLER_SEQUENCE, logger, Fore
from .core import CoroutineSpeedup


class SubscribesCleaner(CoroutineSpeedup):
    """解耦清洗插件：国内IP调用很可能出现性能滑坡"""

    def __init__(self, debug=False, kill_target: str = None):
        super(SubscribesCleaner, self).__init__()
        self.debug = debug
        self.keys = [REDIS_SECRET_KEY.format(s) for s in CRAWLER_SEQUENCE]
        self.rc = RedisClient().get_driver()
        self.kill_ = kill_target

    def offload_task(self):
        for key_ in self.keys:
            try:
                for sub, _ in self.rc.hgetall(key_).items():
                    self.work_q.put_nowait([sub, key_])
            except ResponseError:
                logger.critical("Link pool is broken down.")

    def _del_subs(self, key_: str, subs: str, err_) -> None:
        try:
            self.rc.hdel(key_, subs)
            # logger.debug(f'>> Detach -> {subs} -- {err_}')
            print(Fore.BLUE, f"[{datetime.now()}] detach -> {subs} {err_}")
        except ConnectionError:
            logger.critical("<SubscribeCleaner> The local network communication is abnormal.")

    def control_driver(self, sub_info: List[str], threshold: int = 4):
        """

        :param sub_info: [subs,key_secret_class]
        :param threshold: 解耦置信阈值 小于或等于这个值的订阅将被剔除
        :return:
        """
        try:
            # 针对指定订阅源进行清洗工作
            if self.kill_ and self.kill_ in sub_info[0]:
                self._del_subs(sub_info[-1], sub_info[0], "target active removal")
            else:
                # 解析订阅
                node_info: dict = subs2node(sub_info[0])
                # 订阅解耦
                if node_info['node'].__len__() <= threshold:
                    self._del_subs(sub_info[-1], sub_info[0], "decouple active removal")
                elif self.debug:
                    print(Fore.WHITE, f"[{datetime.now()}] valid -- {node_info['subs']} -- {len(node_info['node'])}")

        except (UnicodeDecodeError, TypeError) as e:
            # 对于已标记“解析错误”的订阅 更新其请求次数
            if self.temp_cache.get(sub_info[0]):
                self.temp_cache[sub_info[0]] += 1
            # 否则标记为“解析错误”的订阅
            else:
                print(Fore.YELLOW, f"[{datetime.now()}] recheck -- {sub_info[0]}")
                self.temp_cache[sub_info[0]] = 1
            # 若链接重试次数少于3次 重添加至任务队列尾部
            if self.temp_cache[sub_info[0]] <= 3:
                self.work_q.put_nowait(sub_info)
            # 若链接重试次数大于3次 剔除
            else:
                self._del_subs(sub_info[-1], sub_info[0], e)
        except SystemExit:
            warnings.warn("请关闭系统代理后部署订阅清洗任务")
        except Exception as e:
            logger.warning(f"{sub_info} -- {e}")
            self._del_subs(sub_info[-1], sub_info[0], e)

    def killer(self):
        if not self.debug:
            logger.success("<SubscribesCleaner> --> decouple compete.")


def subs2node(subs: str, timeout: int = None) -> dict:
    """
    将订阅链接解析成节点数据
    一般订阅解包后，两条信息分别用于描述“可用剩余时长”与"可用剩余流量"，其加密特征与节点完全不同
    :param subs: any class_ subscribe 需要解析的订阅链接
    :param timeout: 设置requests 超时时间
    :return: {'subs': subscribe, "node": info}
    """

    # 订阅类型
    class_ = ''
    headers = {
        'User-Agent': "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko)"
                      " Chrome/88.0.4324.96 Safari/537.36 Edg/88.0.705.53"}
    # 流量不通过系统代理
    proxies = {
        'http': None,
        'https': None
    }
    try:
        # 订阅解析
        obj = urlparse(subs)
        # 类型粗识别
        if '1' in obj.query:
            class_ = 'ssr'
        elif '3' in obj.query:
            class_ = 'v2ray'

        obj_analyze = {'net': obj.netloc, 'token': obj.path.split('/')[-1], 'class_': class_}
        # 根据是否设置超时选择不同的请求方式
        if timeout:
            res = requests.get(subs, headers=headers, timeout=timeout)
        else:
            # res = requests.get(subs, headers=headers)
            res = requests.get(subs, headers=headers, proxies=proxies)
        # 解码订阅链接所指向的服务器节点数据
        node_info = base64.decodebytes(res.content)

        return {'subs': subs, 'info': obj_analyze, "node": [i for i in node_info.decode("utf8").split("\n") if i]}
    # 捕获异常输入 剔除恶意链接或脏数据
    except requests.exceptions.MissingSchema:
        print(f'{subs} -- 传入的subs格式有误或不是订阅链接')
    # 链接清洗任务不能使用代理IP操作
    except requests.exceptions.ProxyError:
        raise SystemExit
    # 并发数过大 远程主机关闭通信窗口
    except requests.exceptions.ConnectionError:
        raise TypeError
    # 未知风险
    except requests.exceptions.RequestException as e:
        print(f"{subs} -- {e}")
