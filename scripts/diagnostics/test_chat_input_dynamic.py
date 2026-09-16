"""
test_chat_input_dynamic.py - ChatInputField 动态出现验证
Phase 2.1: P0.1 - 打开聊天后确认 ChatInputField 的出现
"""

import sys, os, json, time, ctypes, ctypes.wintypes
from dataclasses import dataclass, field, asdict
from typing import Optional, List, Dict, Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

# Win32 API
kernel32 = ctypes.windll.kernel32
user32 = ctypes.windll.user32
psapi = ctypes.windll.psapi

PROCESS_ALL_ACCESS = 0x1FFFFF
PROCESS_VM_READ = 0x0010
PROCESS_VM_WRITE = 0x0020
PROCESS_VM_OPERATION = 0x0008

def get_foreground():
    h = user32.GetForegroundWindow()
    pid = ctypes.wintypes.DWORD()
    user32.GetWindowThreadProcessId(h, ctypes.byref(pid))
    buf = ctypes.create_unicode_buffer(256)
    user32.GetWindowTextW(h, buf, 256)
    proc_name = ""
    try:
        h_process = kernel32.OpenProcess(PROCESS_VM_READ, False, pid.value)
        if h_process:
            buf2 = ctypes.create_unicode_buffer(260)
            size = ctypes.wintypes.DWORD(260)
            psapi.GetModuleBaseNameW(h_process, None, buf2, size)
            proc_name = buf2.value
            kernel32.CloseHandle(h_process)
    except:
        proc_name = str(pid.value)
    return {"hwnd": hex(h), "pid": pid.value, "process": proc_name, "title": buf.value.strip()}

def read_process_memory(h_process, address, size=1):
    buf = ctypes.create_string_buffer(size)
    bytes_read = ctypes.wintypes.SIZE_T()
    ok = kernel32.ReadProcessMemory(h_process, address, buf, size, ctypes.byref(bytes_read))
    if ok and bytes_read.value == size:
        return buf.raw[0]
    return None

def write_process_memory(h_process, address, value):
    buf = ctypes.c_byte(value)
    bytes_written = ctypes.wintypes.SIZE_T()
    ok = kernel32.WriteProcessMemory(h_process, address, ctypes.byref(buf), 1, ctypes.byref(bytes_written))
    return ok and bytes_written.value == 1

def activate_gate(pid):
    h_process = kernel32.OpenProcess(PROCESS_ALL_ACCESS, False, pid)
    if not h_process:
        h_process = kernel32.OpenProcess(PROCESS_VM_READ | PROCESS_VM_WRITE | PROCESS_VM_OPERATION, False, pid)
    if not h_process:
        print(f"  Cannot open process {pid}")
        return False
    
    # Find Weixin.dll base
    try:
        import psutil
        proc = psutil.Process(pid)
        dll_base = 0
        for mmap in proc.memory_maps():
            if "Weixin.dll" in mmap.path:
                dll_base = int(mmap.addr, 16) if isinstance(mmap.addr, str) else mmap.addr
                break
        if not dll_base:
            for module in proc.memory_maps():
                p = module.path
                if p and "Weixin.dll" in p:
                    dll_base = int(module.addr, 16) if isinstance(module.addr, str) else module.addr
                    break
    except:
        dll_base = 0
    
    if not dll_base:
        # Fallback: use GetModuleInformation
        h_modules = (ctypes.wintypes.HMODULE * 1024)()
        cb_needed = ctypes.wintypes.DWORD()
        if psapi.EnumProcessModules(h_process, h_modules, ctypes.sizeof(h_modules), ctypes.byref(cb_needed)):
            count = cb_needed.value // ctypes.sizeof(ctypes.wintypes.HMODULE)
            for i in range(count):
                mod_name = ctypes.create_unicode_buffer(260)
                psapi.GetModuleBaseNameW(h_process, h_modules[i], mod_name, 260)
                if "Weixin" in mod_name.value:
                    dll_base = h_modules[i]
                    break
    
    if not dll_base:
        print("  Cannot find Weixin.dll")
        kernel32.CloseHandle(h_process)
        return False
    
    rva = 0xad19668
    gate_addr = dll_base + rva
    old_val = read_process_memory(h_process, gate_addr)
    print(f"  Gate at 0x{gate_addr:x}: old={old_val}")
    
    if old_val == 0:
        write_process_memory(h_process, gate_addr, 1)
        new_val = read_process_memory(h_process, gate_addr)
        print(f"  Gate: {old_val} -> {new_val}")
    else:
        print(f"  Gate already active: {old_val}")
    
    kernel32.CloseHandle(h_process)
    return True


# ============= MAIN =============
print("=" * 60)
print("ChatInputField Dynamic Validation")
print("=" * 60)

# 1. Find WeChat
import psutil
wechat_procs = [p for p in psutil.process_iter(['pid', 'name', 'exe']) if p.info['name'] == 'Weixin.exe']
if not wechat_procs:
    print("ERROR: WeChat not running")
    sys.exit(1)
proc = wechat_procs[0]
pid = proc.info['pid']
print(f"\nWeChat PID={pid} Path={proc.info['exe']}")

# 2. Activate gate
print("\n--- Activating accessibility gate ---")
activate_gate(pid)

# 3. UIA scanning
import pythoncom
import clr
clr.AddReference('UIAutomationClient')
clr.AddReference('UIAutomationTypes')
from System.Windows.Automation import AutomationElement, Condition, TreeScope, \
    PropertyCondition, ControlType, Automation

# Initialize
pythoncom.CoInitialize()

# 4. Get WeChat window
all_windows = AutomationElement.RootElement.FindAll(TreeScope.Children, Condition.TrueCondition)
wechat_elem = None
for w in all_windows:
    try:
        cls = w.Current.ClassName
        if cls and 'Qt' in cls and w.Current.ProcessId == pid:
            wechat_elem = w
            print(f"\nWeChat window: Name='{w.Current.Name}' Class='{cls}'")
            break
    except:
        pass

if not wechat_elem:
    # Try by handle
    hwnd = None
    for p in psutil.process_iter(['pid']):
        if p.info['pid'] == pid:
            try:
                hwnd = p._proc.handle_info().hwnd
            except:
                pass
    if not hwnd:
        # Use FindWindow
        hwnd = user32.FindWindowW(None, "微信")
    if hwnd:
        wechat_elem = AutomationElement.FromHandle(hwnd)
        print(f"\nWeChat window (by handle): Name='{wechat_elem.Current.Name}'")

if not wechat_elem:
    print("ERROR: Cannot find WeChat UIA element")
    sys.exit(1)

# 5. Save tree BEFORE open_chat
def walk_tree(elem, depth=0, max_depth=5):
    """Walk UIA tree and return structured data"""
    result = []
    try:
        name = elem.Current.Name
        cls = elem.Current.ClassName
        ctrl = elem.Current.ControlType.ProgrammaticName
        enabled = elem.Current.IsEnabled
        aid = elem.Current.AutomationId
        rect = elem.Current.BoundingRectangle
        result.append({
            "name": name,
            "class_name": cls,
            "control_type": ctrl,
            "enabled": enabled,
            "automation_id": aid,
            "depth": depth,
        })
        if depth < max_depth:
            children = elem.FindAll(TreeScope.Children, Condition.TrueCondition)
            for c in children:
                result.extend(walk_tree(c, depth + 1, max_depth))
    except:
        pass
    return result

print("\n--- Scanning UIA tree BEFORE open_chat ---")
tree_before = walk_tree(wechat_elem, 0, 5)
with open(os.path.join(os.path.dirname(__file__), '../../docs/evidence/tree_before_open_chat.json'), 'w', encoding='utf-8') as f:
    json.dump(tree_before, f, ensure_ascii=False, indent=2)
print(f"Tree saved: {len(tree_before)} nodes")

# Count mmui nodes
mmui_before = [n for n in tree_before if 'mmui::' in n.get('class_name', '')]
input_before = [n for n in tree_before if 'Input' in n.get('class_name', '') or 'Edit' in n.get('class_name', '')]
print(f"  mmui nodes: {len(mmui_before)}")
print(f"  input-like nodes: {len(input_before)}")
for n in input_before:
    print(f"    {n['class_name']} name='{n['name']}'")

# 6. Open chat - we need to use the UIA to open a chat
# First, find the session list
print("\n--- Finding session list ---")
session_list = None
for attempt in range(3):
    try:
        list_cond = PropertyCondition(AutomationElement.ClassNameProperty, "mmui::ChatSessionList")
        session_list = wechat_elem.FindFirst(TreeScope.Descendants, list_cond)
        if session_list:
            cells = session_list.FindAll(TreeScope.Children, Condition.TrueCondition)
            print(f"  ChatSessionList found with {cells.Count} children")
            for i in range(min(cells.Count, 10)):
                c = cells[i]
                print(f"  [{i}] Name='{c.Current.Name[:50]}'")
            break
    except:
        pass
    time.sleep(0.5)

if not session_list:
    print("ERROR: Cannot find session list")
    # Try to find by other means
    cell_cond = PropertyCondition(AutomationElement.ClassNameProperty, "mmui::ChatSessionCell")
    cells = wechat_elem.FindAll(TreeScope.Descendants, cell_cond)
    print(f"  Found {cells.Count} ChatSessionCell directly")

# Find "好友B" 
cell_cond = PropertyCondition(AutomationElement.ClassNameProperty, "mmui::ChatSessionCell")
all_cells = wechat_elem.FindAll(TreeScope.Descendants, cell_cond)
target_cell = None
for i in range(all_cells.Count):
    name = all_cells[i].Current.Name
    if '好友B' in name:
        target_cell = all_cells[i]
        print(f"\n  Found target cell[{i}]: '{name[:50]}'")
        break

if not target_cell:
    print("ERROR: Cannot find '好友B' cell")
    # Use first available cell
    if all_cells.Count > 0:
        target_cell = all_cells[0]
        print(f"  Using first cell instead: '{all_cells[0].Current.Name[:50]}'")
    else:
        print("ERROR: No cells at all")
        sys.exit(1)

# 7. Open chat using SelectionItemPattern
print(f"\n--- Opening chat: '{target_cell.Current.Name[:30]}...' ---")
fg_before = get_foreground()
print(f"  Foreground before: {fg_before['process']} - {fg_before['title']}")

try:
    sel_pattern = target_cell.GetCurrentPattern(10031)  # SelectionItemPattern ID
    sel_pattern.Select()
    print("  SelectionItemPattern.Select() called")
except Exception as e:
    print(f"  SelectionItemPattern failed: {e}")
    try:
        inv_pattern = target_cell.GetCurrentPattern(10000)  # InvokePattern ID
        inv_pattern.Invoke()
        print("  InvokePattern.Invoke() called")
    except Exception as e2:
        print(f"  InvokePattern also failed: {e2}")

time.sleep(2)

fg_after = get_foreground()
print(f"  Foreground after: {fg_after['process']} - {fg_after['title']}")
print(f"  Foreground changed: {fg_before['hwnd'] != fg_after['hwnd']}")
print(f"  WeChat became foreground: {fg_after['pid'] == pid}")

# 8. Scan tree AFTER open_chat
print("\n--- Scanning UIA tree AFTER open_chat ---")
time.sleep(1)
tree_after = walk_tree(wechat_elem, 0, 5)
with open(os.path.join(os.path.dirname(__file__), '../../docs/evidence/tree_after_open_chat.json'), 'w', encoding='utf-8') as f:
    json.dump(tree_after, f, ensure_ascii=False, indent=2)
print(f"Tree saved: {len(tree_after)} nodes")

mmui_after = [n for n in tree_after if 'mmui::' in n.get('class_name', '')]
input_after = [n for n in tree_after if 'Input' in n.get('class_name', '') or 'Edit' in n.get('class_name', '')]
print(f"  mmui nodes: {len(mmui_after)}")
print(f"  input-like nodes: {len(input_after)}")
for n in input_after:
    print(f"    {n['class_name']} name='{n['name']}'")

# 9. Diff
print("\n--- Tree diff ---")
before_classes = set(n['class_name'] for n in tree_before)
after_classes = set(n['class_name'] for n in tree_after)
new_classes = after_classes - before_classes
lost_classes = before_classes - after_classes
if new_classes:
    print(f"  New classes: {sorted(new_classes)}")
if lost_classes:
    print(f"  Lost classes: {sorted(lost_classes)}")

# Check specifically for ChatInputField
chat_input = [n for n in tree_after if 'ChatInput' in n.get('class_name', '')]
print(f"\n  ChatInputField nodes: {len(chat_input)}")
for n in chat_input:
    print(f"    {n['class_name']} name='{n['name']}' aid='{n['automation_id']}'")

# Check for new XValidatorTextEdit
new_edits = [n for n in tree_after if n.get('class_name') == 'mmui::XValidatorTextEdit' and n not in tree_before]
# Actually just list all XValidatorTextEdit
all_edits = [n for n in tree_after if n.get('class_name') == 'mmui::XValidatorTextEdit']
print(f"\n  XValidatorTextEdit nodes: {len(all_edits)}")
for n in all_edits:
    print(f"    name='{n['name']}'")

# 10. Save chat input field info
chat_input_field = None
for n in tree_after:
    if 'ChatInput' in n.get('class_name', '') or ('XValidatorTextEdit' in n.get('class_name', '') and n.get('name') != '搜索'):
        chat_input_field = n
        break

if chat_input_field:
    print(f"\n  ChatInputField found: {chat_input_field}")
    with open(os.path.join(os.path.dirname(__file__), '../../docs/evidence/chat_input_field.json'), 'w', encoding='utf-8') as f:
        json.dump(chat_input_field, f, ensure_ascii=False, indent=2)
else:
    print("\n  ChatInputField NOT found after open_chat")
    # Maybe the search field is the only input
    if all_edits:
        chat_input_field = all_edits[0]
        print(f"  Using search field as input: {all_edits[0]}")

print("\nDone. Checkpoint 1 complete.")
