"""通用插件注册表 —— 让"新增一种检测器/一个刮削站"变成**加一个文件**，而不是改主流程。

设计要点
--------
1. **按名字注册、按优先级排序**：UI 与配置只认名字（``visual.mosaic_cv``），
   不 import 具体类，所以第三方/未来新增的实现可以零改动接入。
2. **延迟创建**：注册的是"工厂"，只有真正要用到时才实例化 —— 依赖没装的
   插件（例如没装 numpy 的画面检测）可以注册成功但**报告不可用**，而不是让
   整个程序启动失败。
3. **可覆盖**：测试里用 ``replace=True`` 塞假实现，不用 monkeypatch。

同一份代码同时服务两类东西：``detector``（对一部片出证据）与
``source``（对一部作品查网络资源），互不干扰。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Generic, Iterator, TypeVar

T = TypeVar("T")

__all__ = ["PluginSpec", "Registry", "detectors", "sources"]


@dataclass
class PluginSpec(Generic[T]):
    """一条注册记录。"""

    name: str
    factory: Callable[..., T]
    priority: int = 100          # 数字越小越先执行
    title: str = ""              # 界面上显示的名字
    description: str = ""
    tags: tuple[str, ...] = field(default_factory=tuple)

    def create(self, *args, **kwargs) -> T:
        return self.factory(*args, **kwargs)


class Registry(Generic[T]):
    """按名字管理的插件表。"""

    def __init__(self, kind: str) -> None:
        self.kind = kind
        self._items: dict[str, PluginSpec[T]] = {}

    # ── 注册 ──
    def add(
        self,
        name: str,
        factory: Callable[..., T],
        *,
        priority: int = 100,
        title: str = "",
        description: str = "",
        tags: tuple[str, ...] = (),
        replace: bool = False,
    ) -> PluginSpec[T]:
        if name in self._items and not replace:
            raise KeyError(f"{self.kind} 插件重名: {name}（如需覆盖请传 replace=True）")
        spec = PluginSpec(name=name, factory=factory, priority=priority,
                          title=title or name, description=description, tags=tags)
        self._items[name] = spec
        return spec

    def register(
        self,
        name: str,
        *,
        priority: int = 100,
        title: str = "",
        description: str = "",
        tags: tuple[str, ...] = (),
    ) -> Callable[[Callable[..., T]], Callable[..., T]]:
        """装饰器写法：``@detectors.register("visual.mosaic_cv", priority=50)``。"""

        def _decorator(cls_or_fn: Callable[..., T]) -> Callable[..., T]:
            self.add(name, cls_or_fn, priority=priority, title=title,
                     description=description, tags=tags, replace=True)
            return cls_or_fn

        return _decorator

    # ── 查询 ──
    def get(self, name: str) -> PluginSpec[T]:
        try:
            return self._items[name]
        except KeyError as exc:
            raise KeyError(
                f"没有名为 {name!r} 的 {self.kind}，已注册: {sorted(self._items)}"
            ) from exc

    def has(self, name: str) -> bool:
        return name in self._items

    def names(self) -> list[str]:
        return sorted(self._items)

    def specs(self) -> list[PluginSpec[T]]:
        """按优先级排序（同优先级按名字，保证可复现）。"""
        return sorted(self._items.values(), key=lambda s: (s.priority, s.name))

    def create_all(self, *args, **kwargs) -> Iterator[T]:
        """按优先级依次实例化；单个插件构造失败不影响其它插件。"""
        for spec in self.specs():
            try:
                yield spec.create(*args, **kwargs)
            except Exception:  # noqa: BLE001 — 插件坏了不该拖垮主流程
                continue

    def discard(self, name: str) -> bool:
        """移除一条注册（返回是否存在）。测试与"临时禁用某插件"都用它。"""
        return self._items.pop(name, None) is not None

    def clear(self) -> None:
        """仅供测试使用。"""
        self._items.clear()

    def __len__(self) -> int:
        return len(self._items)

    def __contains__(self, name: object) -> bool:
        return name in self._items


#: 有码检测器（对一部片出证据）
detectors: Registry = Registry("detector")

#: 无码资源检索源（对一部作品查网络）
sources: Registry = Registry("source")
