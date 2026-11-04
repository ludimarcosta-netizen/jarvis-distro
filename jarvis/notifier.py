"""
Notificação do Windows (toast) do Jarvis. Clicar abre um endereço (ex.: a reunião na página).

Usa o Windows PowerShell 5.1, que traz as APIs de notificação do Windows sem dependência extra.
A notificação aparece com o nome "Windows PowerShell". Falha aqui nunca derruba quem chamou.
"""
import os
import subprocess
from xml.sax.saxutils import escape, quoteattr

_PS = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "WindowsPowerShell", "v1.0",
                   "powershell.exe")
_APP_ID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"
_SCRIPT = (
    "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null;"
    "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null;"
    "$x = New-Object Windows.Data.Xml.Dom.XmlDocument; $x.LoadXml($env:JARVIS_TOAST_XML);"
    "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($env:JARVIS_TOAST_APP)"
    ".Show((New-Object Windows.UI.Notifications.ToastNotification $x))"
)


def toast_xml(title: str, body: str, url: str, button: str) -> str:
    """Fica na tela até o senhor agir (scenario=reminder); o botão e o clique abrem a url."""
    link = quoteattr(url)
    return (f'<toast activationType="protocol" launch={link} scenario="reminder">'
            f'<visual><binding template="ToastGeneric"><text>{escape(title)}</text><text>{escape(body)}</text>'
            f'</binding></visual><actions>'
            f'<action content={quoteattr(button)} activationType="protocol" arguments={link}/>'
            f'<action content="Agora não" activationType="system" arguments="dismiss"/>'
            f'</actions></toast>')


def notify(title: str, body: str, url: str, button: str = "Abrir") -> bool:
    if os.name != "nt" or not os.path.exists(_PS):
        return False
    env = dict(os.environ, JARVIS_TOAST_XML=toast_xml(title, body, url, button), JARVIS_TOAST_APP=_APP_ID)
    try:
        subprocess.Popen([_PS, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", _SCRIPT],
                         env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=0x08000000)  # CREATE_NO_WINDOW
        return True
    except OSError as exc:
        print(f"[!] Notificação do Windows falhou: {exc}")
        return False
