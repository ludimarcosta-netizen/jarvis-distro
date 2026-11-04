# wa_uia_final.py — Envio VERIFICADO via WhatsApp Web PWA usando pywinauto (UIA in-process).
# Tudo roda DENTRO do python anexado ao desktop de entrada (WinSta0\Default):
#   - SetThreadDesktop -> EnumWindows ve as janelas (comprovado)
#   - activate via AttachThreadInput (comprovado)
#   - UIA via pywinauto/comtypes: busca -> contato -> composer -> enviar -> bolha
# Uso: python wa_uia_final.py "<contato>" "<mensagem>"
import ctypes
import re
import sys
import time
import unicodedata
from ctypes import wintypes
from send_result import classify_send, is_new_exact_message
user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
def log(nome, ok, detalhe=""):
   print(("[OK] " if ok else "[FALHOU] ") + nome + ((" | " + str(detalhe)) if detalhe else ""), flush=True)
   return ok
def attach():
   user32.SetProcessDPIAware()
   h = user32.OpenInputDesktop(0, False, 0x01FF)
   if h:
       user32.SetThreadDesktop(h)
   return bool(h)
def enum_titled():
   wins = []
   def cb(hwnd, _):
       if user32.IsWindowVisible(hwnd):
           length = user32.GetWindowTextLengthW(hwnd)
           if length > 0:
               buf = ctypes.create_unicode_buffer(length + 1)
               user32.GetWindowTextW(hwnd, buf, length + 1)
               t = buf.value.strip()
               if t and t not in ("Default IME", "MSCTFIME UI"):
                   pid = wintypes.DWORD(0)
                   user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                   wins.append({"hwnd": hwnd, "pid": pid.value, "title": t})
       return True
   WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
   user32.EnumWindows(WNDENUMPROC(cb), 0)
   return wins
def find_whatsapp():
   for w in enum_titled():
       t = w["title"].strip().lower()
       if t == "whatsapp" or t == "whatsapp web" or t.endswith("whatsapp") or \
          t.endswith("whatsapp web") or "whatsapp" in t.split(" - ")[0].lower():
           return w
   return None
def activate(hwnd):
   user32.ShowWindow(hwnd, 9)
   for _ in range(6):
       if user32.GetForegroundWindow() == hwnd:
           return True
       time.sleep(0.15)
   fg = user32.GetForegroundWindow()
   pid_fg = wintypes.DWORD(0)
   tid_fg = user32.GetWindowThreadProcessId(fg, ctypes.byref(pid_fg))
   tid_me = kernel32.GetCurrentThreadId()
   user32.AttachThreadInput(tid_me, tid_fg, True)
   try:
       user32.ShowWindow(hwnd, 9)
       user32.BringWindowToTop(hwnd)
       user32.SetForegroundWindow(hwnd)
   finally:
       user32.AttachThreadInput(tid_me, tid_fg, False)
   for _ in range(12):
       if user32.GetForegroundWindow() == hwnd:
           return True
       time.sleep(0.15)
   return False
def norm(s):
   s = unicodedata.normalize("NFKD", s.casefold())
   return " ".join("".join(c for c in s if not unicodedata.combining(c)).split())
def contact_matches(cname, expected):
   """True se o nome do composer corresponde ao contato pedido."""
   rm = re.match(r"^(?:digite uma mensagem para|type a message to|type a message for)\s+(.+)$",
                 (cname or "").strip(), re.IGNORECASE)
   if not rm:
       return False
   contact = re.sub(r"\s+\((voce|você)\)$", "", rm.group(1).strip(), flags=re.IGNORECASE)
   nc, ne = norm(contact), norm(expected)
   return bool(nc) and nc == ne

def send(recipient, message):
   if not log("Anexar ao desktop de entrada", attach()):
       return False, False
   w = find_whatsapp()
   if not log("Localizar janela do WhatsApp (PWA)", bool(w),
              w["title"] if w else "nao encontrada"):
       return False, False
   hwnd = w["hwnd"]
   if not log("Ativar janela em primeiro plano", activate(hwnd)):
       return False, False
   time.sleep(0.8)
   # pywinauto UIA — in-process
   from pywinauto import Desktop
   try:
       wa = Desktop(backend="uia").window(handle=hwnd)
       wa.wait("exists ready", timeout=15)
   except Exception as exc:
       log("Abrir wrapper pywinauto da janela", False)
       return False, False
   log("Wrapper pywinauto pronto", True)
   # ---- 1) Campo de busca de CONTATOS (nao a barra de endereco) ----
   search = None
   search_name = ""
   try:
       edits = wa.descendants(control_type="Edit")
       log("Campos Edit visiveis", True, "%d encontrados" % len(edits))
       for e in edits:
           name = (e.element_info.name or "").lower()
           # busca de contatos do WhatsApp: "pesquisar ou começar nova conversa" etc.
           if ("pesquisar" in name or "search" in name) and "endereco" not in name and "address" not in name:
               search = e
               search_name = e.element_info.name or ""
               break
   except Exception as exc:
       log("Enumerar Edits", False)
       return False, False
   if search is None:
       # fallback: Painel de busca via botao da lupa
       try:
           buttons = wa.descendants(control_type="Button")
           for b in buttons:
               nm = (b.element_info.name or "").strip().lower()
               if nm in ("pesquisar", "search") or "nova conversa" in nm or "new chat" in nm:
                   try:
                       b.invoke()
                       time.sleep(0.8)
                   except Exception:
                       b.click_input()
                       time.sleep(0.8)
                   edits = wa.descendants(control_type="Edit")
                   for e in edits:
                       name = (e.element_info.name or "").lower()
                       if ("pesquisar" in name or "search" in name) and "endereco" not in name and "address" not in name:
                           search = e
                           search_name = e.element_info.name or ""
                           break
                   break
       except Exception as exc:
           log("Fallback lupa", False)
   if not log("Localizar campo de busca de contatos", bool(search),
              "name='%s'" % search_name[:50] if search else "nenhum"):
       return False, False
   try:
       search.set_focus()
       time.sleep(0.4)
       search.type_keys("^a", pause=0.05)
       search.type_keys(recipient, pause=0.02)
       time.sleep(1.5)
       search.type_keys("{ENTER}", pause=0.05)
       time.sleep(1.5)
       log("Buscar contato", True)
   except Exception as exc:
       log("Digitar na busca", False)
       return False, False
   # ---- 2) Campo de mensagem (composer) — conferindo o destinatario ----
   composer = None
   cname = ""
   try:
       edits = wa.descendants(control_type="Edit")
       for e in edits:
           name = e.element_info.name or ""
           if re.search(r"(?i)type a message|digite uma mensagem|mensagem|message", name):
               composer = e
               cname = name
               break
   except Exception as exc:
       log("Enumerar composer", False, str(exc)[:120])
       return False, False
   if not log("Localizar campo de mensagem", bool(composer)):
       return False, False
   if not log("Conferir conversa certa", contact_matches(cname, recipient)):
       return False, False
   try:
       composer.set_focus()
       time.sleep(0.3)
   except Exception as exc:
       log("Focar composer", False)
       return False, False
   # ---- 3) Contar bolhas antes ----
   def bubble_texts():
       try:
           texts = wa.descendants(control_type="Text")
           return [(t.element_info.name or "") for t in texts]
       except Exception:
           return None
   before = bubble_texts()
   # ---- 4) Colar mensagem via CLIPBOARD (type_keys engole espacos) e enviar ----
   import base64
   import subprocess as _sp
   b64 = base64.b64encode(message.encode("utf-8")).decode("ascii")
   _sp.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
            "$t=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('%s')); Set-Clipboard -Value $t" % b64],
           capture_output=True, text=True, timeout=10)
   time.sleep(0.2)
   try:
       composer.set_focus()
       time.sleep(0.2)
       composer.type_keys("^v", pause=0.05)
       time.sleep(0.6)
   except Exception as exc:
       log("Colar mensagem no composer", False)
       return False, False
   clicked = False
   try:
       buttons = wa.descendants(control_type="Button")
       for b in buttons:
           try:
               nm = (b.element_info.name or "").strip().lower()
               aid = (b.element_info.automation_id or "").lower()
               if re.match(r"^(enviar|send)( mensagem| message)?$", nm) or "send" in aid:
                   b.invoke()
                   clicked = True
                   break
           except Exception:
               continue
   except Exception as exc:
       log("Enumerar botao Enviar", False)
       return False, False
   if not clicked:
       # fallback: Enter no composer
       try:
           composer.type_keys("{ENTER}", pause=0.05)
           clicked = True
       except Exception as exc:
           log("Acionar envio", False)
           return False, False
   log("Acionar botao Enviar", clicked)
   # A confirmação exige uma bolha exata que apareceu após o acionamento; a prévia
   # da lista de conversas pode ser antiga ou pertencer a um contato semelhante.
   appeared = False
   for _ in range(5):
       time.sleep(0.8)
       current = bubble_texts()
       if is_new_exact_message(message, before, current):
           appeared = True
           break
   log("Bolha nova da mensagem confirmada", appeared)
   return clicked, appeared
def main():
   if len(sys.argv) < 3:
       print('Uso: python wa_uia_final.py "<contato>" "<mensagem>"')
       sys.exit(1)
   recipient, message = sys.argv[1], sys.argv[2]
   print("=== Verificação de envio WhatsApp ===", flush=True)
   attempted, appeared = send(recipient, message)
   state = classify_send(attempted, appeared)
   if state == "confirmed":
       print("RESULTADO: envio confirmado por bolha nova na conversa.")
       sys.exit(0)
   elif state == "triggered_unconfirmed":
       print("RESULTADO: acionado, confirmação ausente; não repetir automaticamente.")
       sys.exit(3)
   else:
       print("RESULTADO: falha antes do acionamento.")
       sys.exit(2)
if __name__ == "__main__":
   main()
