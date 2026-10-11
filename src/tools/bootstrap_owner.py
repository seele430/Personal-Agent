
"""Personal Agent - 安全初始化站长账户。"""

import ctypes
import getpass
import sys
import tkinter as tk

from src.auth_api import AUTH_DB_PATH
from src.core.auth_store import (
    AuthError,
    AuthForbidden,
    AuthStore,
)


MAX_PASSWORD_ATTEMPTS = 3

BG = "#F3F6FB"
WHITE = "#FFFFFF"
NAVY = "#202D45"
MUTED = "#7B879B"
BORDER = "#DCE3ED"
PRIMARY = "#426CF5"
DANGER = "#C73E4D"
SUCCESS = "#25845D"

FONT = "Microsoft YaHei UI"


def validate_password(password: str) -> None:
    if not isinstance(password, str):
        raise AuthError("密码格式不正确")

    if not password or not password.strip():
        raise AuthError("密码不能为空")

    length = len(password.encode("utf-8"))

    if length < 12:
        raise AuthError("密码至少需要12字节")

    if length > 1024:
        raise AuthError("密码不能超过1024字节")


def create_owner(
    store: AuthStore,
    username: str,
    password: str,
    confirmation: str,
) -> dict:

    validate_password(password)

    if password != confirmation:
        raise AuthError("两次输入的密码不一致")

    return store.bootstrap_owner(
        username,
        password,
    )


# 保留用于非 Windows 终端及现有测试
def prompt_password_pair(
    max_attempts: int = MAX_PASSWORD_ATTEMPTS,
) -> tuple[str, str]:

    for attempt in range(1, max_attempts + 1):
        print(
            f"密码输入尝试：{attempt}/{max_attempts}"
        )

        password = getpass.getpass(
            "请输入密码："
        )

        try:
            validate_password(password)
        except AuthError as exc:
            print(f"输入无效：{exc}")
            continue

        confirmation = getpass.getpass(
            "请确认密码："
        )

        if password != confirmation:
            print("两次密码不一致")
            continue

        return password, confirmation

    raise AuthError("密码输入失败次数过多")


# ======================================
# Windows 键盘布局检查
# ======================================

def english_keyboard_active() -> bool:
    if sys.platform != "win32":
        return True

    user32 = ctypes.WinDLL(
        "user32",
        use_last_error=True,
    )

    user32.GetKeyboardLayout.argtypes = [
        ctypes.c_uint32
    ]
    user32.GetKeyboardLayout.restype = (
        ctypes.c_void_p
    )

    layout = user32.GetKeyboardLayout(0)

    if layout is None:
        return False

    # 主语言标识 0x09 表示英语
    language = int(layout) & 0x03FF

    return language == 0x09


def disable_ime_for_widget(widget) -> bool:
    """尝试移除 Windows IMM 输入法上下文。"""

    if sys.platform != "win32":
        return True

    try:
        imm32 = ctypes.WinDLL(
            "imm32",
            use_last_error=True,
        )

        func = imm32.ImmAssociateContextEx

        func.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_uint32,
        ]
        func.restype = ctypes.c_int

        return bool(
            func(
                ctypes.c_void_p(widget.winfo_id()),
                None,
                0,
            )
        )

    except (OSError, AttributeError, tk.TclError):
        return False


# ======================================
# 图形密码窗口
# ======================================

def prompt_password_gui(
    username: str,
) -> tuple[str, str]:

    root = tk.Tk()

    root.title("Personal Agent · 安全初始化")
    root.geometry("540x510")
    root.resizable(False, False)
    root.configure(bg=BG)

    result = []

    password_var = tk.StringVar(master=root)
    confirm_var = tk.StringVar(master=root)
    status_var = tk.StringVar(
        master=root,
        value="请先切换到英语键盘布局。",
    )

    # 居中窗口
    root.update_idletasks()

    screen_w = root.winfo_screenwidth()
    screen_h = root.winfo_screenheight()

    x = (screen_w - 540) // 2
    y = (screen_h - 510) // 2

    root.geometry(f"540x510+{x}+{y}")

    # ==================================
    # 页面标题
    # ==================================

    outer = tk.Frame(
        root,
        bg=BG,
    )
    outer.pack(
        fill="both",
        expand=True,
        padx=26,
        pady=24,
    )

    tk.Label(
        outer,
        text="PERSONAL AGENT",
        font=(FONT, 11, "bold"),
        fg=PRIMARY,
        bg=BG,
    ).pack(anchor="w")

    tk.Label(
        outer,
        text="保护你的管理员账户",
        font=(FONT, 20, "bold"),
        fg=NAVY,
        bg=BG,
    ).pack(
        anchor="w",
        pady=(9, 5),
    )

    tk.Label(
        outer,
        text="创建本机站长密码 · 仅用于首次部署",
        font=(FONT, 10),
        fg=MUTED,
        bg=BG,
    ).pack(
        anchor="w",
        pady=(0, 18),
    )

    # ==================================
    # 白色卡片
    # ==================================

    card = tk.Frame(
        outer,
        bg=WHITE,
        highlightbackground=BORDER,
        highlightthickness=1,
    )

    card.pack(
        fill="both",
        expand=True,
    )

    content = tk.Frame(
        card,
        bg=WHITE,
    )

    content.pack(
        fill="both",
        expand=True,
        padx=24,
        pady=20,
    )

    tk.Label(
        content,
        text=f"站长账户   {username}",
        font=(FONT, 11, "bold"),
        fg=NAVY,
        bg=WHITE,
    ).pack(
        anchor="w",
        pady=(0, 15),
    )

    # ==================================
    # 密码输入框
    # ==================================

    tk.Label(
        content,
        text="设置密码",
        font=(FONT, 10, "bold"),
        fg=NAVY,
        bg=WHITE,
    ).pack(anchor="w")

    password_entry = tk.Entry(
        content,
        textvariable=password_var,
        show="*",
        font=(FONT, 12),
        bg="#F7F9FC",
        fg=NAVY,
        insertbackground=NAVY,
        relief="flat",
        highlightthickness=1,
        highlightbackground=BORDER,
        highlightcolor=PRIMARY,
        state="disabled",
    )

    password_entry.pack(
        fill="x",
        ipady=9,
        pady=(7, 12),
    )

    tk.Label(
        content,
        text="确认密码",
        font=(FONT, 10, "bold"),
        fg=NAVY,
        bg=WHITE,
    ).pack(anchor="w")

    confirm_entry = tk.Entry(
        content,
        textvariable=confirm_var,
        show="*",
        font=(FONT, 12),
        bg="#F7F9FC",
        fg=NAVY,
        insertbackground=NAVY,
        relief="flat",
        highlightthickness=1,
        highlightbackground=BORDER,
        highlightcolor=PRIMARY,
        state="disabled",
    )

    confirm_entry.pack(
        fill="x",
        ipady=9,
        pady=(7, 12),
    )

    tk.Label(
        content,
        text="建议至少16位，使用独立的英文字母、数字和符号",
        font=(FONT, 9),
        fg=MUTED,
        bg=WHITE,
    ).pack(
        anchor="w",
        pady=(0, 12),
    )

    # ==================================
    # 输入法安全状态
    # ==================================

    status_label = tk.Label(
        content,
        textvariable=status_var,
        font=(FONT, 9),
        fg=DANGER,
        bg=WHITE,
        anchor="w",
        justify="left",
        wraplength=415,
    )

    status_label.pack(
        fill="x",
        pady=(0, 12),
    )

    def set_status(message, color=DANGER):
        status_var.set(message)
        status_label.configure(fg=color)

    def lock_inputs():
        password_entry.configure(state="disabled")
        confirm_entry.configure(state="disabled")

        password_var.set("")
        confirm_var.set("")

    def check_keyboard():
        if not english_keyboard_active():
            lock_inputs()

            set_status(
                "检测到非英语键盘布局。"
                "请按 Win + Space 切换英语键盘后重试。"
            )
            return

        # 第二道防护：请求 Windows 移除 IME 上下文
        ime_a = disable_ime_for_widget(
            password_entry
        )

        ime_b = disable_ime_for_widget(
            confirm_entry
        )

        password_entry.configure(state="normal")
        confirm_entry.configure(state="normal")

        if ime_a and ime_b:
            set_status(
                "英语键盘已确认，可以输入密码。",
                SUCCESS,
            )
        else:
            set_status(
                "英语键盘已确认；无法验证输入法禁用状态。"
                "请确保没有候选词窗口再输入。",
                DANGER,
            )

        password_entry.focus_set()

    # 密码输入期间如果切换回中文键盘，
    # 中断输入并清除当前字段。
    def guard_keyboard(_event):
        if not english_keyboard_active():
            lock_inputs()

            set_status(
                "输入法已切换，密码输入已锁定。"
                "请切回英语键盘并重新检查。"
            )

            return "break"

        return None

    password_entry.bind(
        "<KeyPress>",
        guard_keyboard,
    )
    confirm_entry.bind(
        "<KeyPress>",
        guard_keyboard,
    )

    # ==================================
    # 检查键盘按钮
    # ==================================

    tk.Button(
        content,
        text="检查英语键盘并启用密码框",
        command=check_keyboard,
        font=(FONT, 10),
        fg=PRIMARY,
        bg="#ECF1FF",
        activebackground="#DFE8FF",
        relief="flat",
        cursor="hand2",
        padx=12,
        pady=8,
    ).pack(
        fill="x",
        pady=(0, 15),
    )

    def submit():
        if not english_keyboard_active():
            lock_inputs()
            set_status("请先切换英语键盘。")
            return

        password = password_var.get()
        confirmation = confirm_var.get()

        try:
            validate_password(password)

            if not confirmation:
                raise AuthError("请填写确认密码")

            if password != confirmation:
                raise AuthError(
                    "两次密码不一致，请重新确认"
                )

        except AuthError as exc:
            set_status(str(exc))
            return

        result.append(
            (password, confirmation)
        )

        # 清空 GUI 控件中的密码
        password_var.set("")
        confirm_var.set("")

        root.destroy()

    def cancel():
        password_var.set("")
        confirm_var.set("")
        root.destroy()

    # ==================================
    # 操作按钮
    # ==================================

    actions = tk.Frame(
        outer,
        bg=BG,
    )

    actions.pack(
        fill="x",
        pady=(15, 0),
    )

    tk.Button(
        actions,
        text="取消",
        command=cancel,
        font=(FONT, 10),
        fg=MUTED,
        bg=WHITE,
        relief="flat",
        padx=24,
        pady=10,
        cursor="hand2",
    ).pack(side="left")

    tk.Button(
        actions,
        text="确认创建账户",
        command=submit,
        font=(FONT, 10, "bold"),
        fg=WHITE,
        bg=PRIMARY,
        activeforeground=WHITE,
        activebackground="#3155D6",
        relief="flat",
        padx=25,
        pady=10,
        cursor="hand2",
    ).pack(side="right")

    root.protocol("WM_DELETE_WINDOW", cancel)

    # 不使用 Enter 作为提交快捷键，
    # 避免输入法确认候选词时误触发提交。
    root.after(150, check_keyboard)

    root.mainloop()

    if not result:
        raise AuthError("已取消密码设置")

    return result[0]


# ======================================
# 主入口
# ======================================

def main() -> int:
    print("=" * 48)
    print("Personal Agent - 初始化站长")
    print("=" * 48)

    print(f"账户数据库：{AUTH_DB_PATH}")

    try:
        username = input(
            "请输入站长用户名："
        ).strip()

        if not username:
            raise AuthError("用户名不能为空")

        if sys.platform == "win32":
            password, confirmation = (
                prompt_password_gui(username)
            )
        else:
            password, confirmation = (
                prompt_password_pair()
            )

        store = AuthStore(AUTH_DB_PATH)

        user = create_owner(
            store,
            username,
            password,
            confirmation,
        )

    except (
        AuthError,
        AuthForbidden,
        KeyboardInterrupt,
        EOFError,
    ) as exc:
        print(f"初始化未完成：{exc}")
        return 1

    print()
    print("站长账户创建成功！")
    print(f"用户名：{user['username']}")
    print(f"权限：{user['role']}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())