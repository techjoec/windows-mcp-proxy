"""PowerShell UI Automation scripts used by proxy helper tools."""

from __future__ import annotations

import base64
import json
from typing import Any


def _ps_bool(value: bool) -> str:
    return "$true" if value else "$false"


def encode_powershell(script: str) -> str:
    """Return a powershell.exe command with the script encoded as UTF-16LE."""
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    return f"powershell.exe -NoProfile -ExecutionPolicy Bypass -EncodedCommand {encoded}"


def script_base64(script: str) -> str:
    """Return a base64 UTF-8 representation suitable for staged .ps1 writes."""
    return base64.b64encode(script.encode("utf-8")).decode("ascii")


def snapshot_script(
    *,
    max_depth: int,
    max_nodes: int,
    include_offscreen: bool,
) -> str:
    return rf"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes

$MaxDepth = {max_depth}
$MaxNodes = {max_nodes}
$IncludeOffscreen = {_ps_bool(include_offscreen)}
$script:NodeCount = 0
$script:Truncated = $false

function RectToObj($rect) {{
    if ($null -eq $rect -or $rect.IsEmpty) {{ return $null }}
    return [ordered]@{{
        left = [int][Math]::Round($rect.Left)
        top = [int][Math]::Round($rect.Top)
        right = [int][Math]::Round($rect.Right)
        bottom = [int][Math]::Round($rect.Bottom)
        width = [int][Math]::Round($rect.Width)
        height = [int][Math]::Round($rect.Height)
        centerX = [int][Math]::Round(($rect.Left + $rect.Right) / 2)
        centerY = [int][Math]::Round(($rect.Top + $rect.Bottom) / 2)
    }}
}}

function ControlTypeName($controlType) {{
    if ($null -eq $controlType) {{ return $null }}
    return ($controlType.ProgrammaticName -replace '^ControlType\.', '')
}}

function RuntimeId($element) {{
    try {{
        $id = $element.GetRuntimeId()
        if ($null -eq $id) {{ return $null }}
        return [string]::Join('.', $id)
    }} catch {{
        return $null
    }}
}}

function TryPattern($element, $pattern) {{
    $patternObj = $null
    if ($element.TryGetCurrentPattern($pattern, [ref]$patternObj)) {{
        return $patternObj
    }}
    return $null
}}

function PatternInfo($element) {{
    $patterns = [ordered]@{{}}

    $toggle = TryPattern $element ([System.Windows.Automation.TogglePattern]::Pattern)
    if ($null -ne $toggle) {{
        $patterns.toggle = [ordered]@{{
            available = $true
            state = $toggle.Current.ToggleState.ToString()
        }}
    }}

    $expand = TryPattern $element ([System.Windows.Automation.ExpandCollapsePattern]::Pattern)
    if ($null -ne $expand) {{
        $patterns.expandCollapse = [ordered]@{{
            available = $true
            state = $expand.Current.ExpandCollapseState.ToString()
        }}
    }}

    $selection = TryPattern $element ([System.Windows.Automation.SelectionItemPattern]::Pattern)
    if ($null -ne $selection) {{
        $patterns.selectionItem = [ordered]@{{
            available = $true
            isSelected = [bool]$selection.Current.IsSelected
        }}
    }}

    $value = TryPattern $element ([System.Windows.Automation.ValuePattern]::Pattern)
    if ($null -ne $value) {{
        $text = [string]$value.Current.Value
        if ($text.Length -gt 200) {{ $text = $text.Substring(0, 200) }}
        $patterns.value = [ordered]@{{
            available = $true
            isReadOnly = [bool]$value.Current.IsReadOnly
            value = $text
        }}
    }}

    $invoke = TryPattern $element ([System.Windows.Automation.InvokePattern]::Pattern)
    if ($null -ne $invoke) {{
        $patterns.invoke = [ordered]@{{ available = $true }}
    }}

    return $patterns
}}

function DescribeElement($element, $depth) {{
    if ($script:NodeCount -ge $MaxNodes) {{
        $script:Truncated = $true
        return $null
    }}

    $script:NodeCount += 1
    $node = [ordered]@{{
        runtimeId = RuntimeId $element
        name = [string]$element.Current.Name
        automationId = [string]$element.Current.AutomationId
        className = [string]$element.Current.ClassName
        controlType = ControlTypeName $element.Current.ControlType
        localizedControlType = [string]$element.Current.LocalizedControlType
        processId = [int]$element.Current.ProcessId
        bounds = RectToObj $element.Current.BoundingRectangle
        isEnabled = [bool]$element.Current.IsEnabled
        isOffscreen = [bool]$element.Current.IsOffscreen
        hasKeyboardFocus = [bool]$element.Current.HasKeyboardFocus
        patterns = PatternInfo $element
    }}

    if ($depth -lt $MaxDepth) {{
        $children = @()
        $walker = [System.Windows.Automation.TreeWalker]::ControlViewWalker
        $child = $walker.GetFirstChild($element)
        while ($null -ne $child) {{
            if ($IncludeOffscreen -or -not [bool]$child.Current.IsOffscreen) {{
                $childNode = DescribeElement $child ($depth + 1)
                if ($null -ne $childNode) {{ $children += $childNode }}
            }}
            if ($script:NodeCount -ge $MaxNodes) {{
                $script:Truncated = $true
                break
            }}
            $child = $walker.GetNextSibling($child)
        }}
        if ($children.Count -gt 0) {{ $node.children = $children }}
    }}

    return $node
}}

function FocusRoot() {{
    $focus = [System.Windows.Automation.AutomationElement]::FocusedElement
    if ($null -eq $focus) {{ return @($null, $null) }}

    $walker = [System.Windows.Automation.TreeWalker]::ControlViewWalker
    $current = $focus
    $window = $null
    while ($null -ne $current) {{
        if ($current.Current.ControlType -eq [System.Windows.Automation.ControlType]::Window) {{
            $window = $current
        }}
        $current = $walker.GetParent($current)
    }}
    if ($null -eq $window) {{ $window = $focus }}
    return @($focus, $window)
}}

$focusAndRoot = FocusRoot
$focus = $focusAndRoot[0]
$root = $focusAndRoot[1]

if ($null -eq $root) {{
    [ordered]@{{
        ok = $false
        error = 'No focused UI Automation element.'
    }} | ConvertTo-Json -Depth 8
    exit 0
}}

$rootNode = DescribeElement $root 0

[ordered]@{{
    ok = $true
    source = 'System.Windows.Automation'
    note = 'Use UIA pattern states for toggle/expand/select state. Bounds are whole-element rectangles; sub-glyph coordinates are only available when exposed as child controls.'
    focusedRuntimeId = RuntimeId $focus
    truncated = $script:Truncated
    maxDepth = $MaxDepth
    maxNodes = $MaxNodes
    root = $rootNode
    nodeCount = $script:NodeCount
}} | ConvertTo-Json -Depth 80
"""


def action_script(
    *,
    action: str,
    criteria: dict[str, Any],
    max_depth: int,
    max_nodes: int,
) -> str:
    criteria_json = json.dumps(criteria, separators=(",", ":"))
    criteria_b64 = base64.b64encode(criteria_json.encode("utf-8")).decode("ascii")
    action_json = json.dumps(action)

    return rf"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes

$Action = {action_json}
$MaxDepth = {max_depth}
$MaxNodes = {max_nodes}
$CriteriaJson = [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{criteria_b64}'))
$Criteria = $CriteriaJson | ConvertFrom-Json
$script:Visited = 0
$script:Matches = @()

function ControlTypeName($controlType) {{
    if ($null -eq $controlType) {{ return $null }}
    return ($controlType.ProgrammaticName -replace '^ControlType\.', '')
}}

function RuntimeId($element) {{
    try {{
        $id = $element.GetRuntimeId()
        if ($null -eq $id) {{ return $null }}
        return [string]::Join('.', $id)
    }} catch {{
        return $null
    }}
}}

function RectToObj($rect) {{
    if ($null -eq $rect -or $rect.IsEmpty) {{ return $null }}
    return [ordered]@{{
        left = [int][Math]::Round($rect.Left)
        top = [int][Math]::Round($rect.Top)
        right = [int][Math]::Round($rect.Right)
        bottom = [int][Math]::Round($rect.Bottom)
        centerX = [int][Math]::Round(($rect.Left + $rect.Right) / 2)
        centerY = [int][Math]::Round(($rect.Top + $rect.Bottom) / 2)
    }}
}}

function TryPattern($element, $pattern) {{
    $patternObj = $null
    if ($element.TryGetCurrentPattern($pattern, [ref]$patternObj)) {{
        return $patternObj
    }}
    return $null
}}

function StateInfo($element) {{
    $patterns = [ordered]@{{}}
    $toggle = TryPattern $element ([System.Windows.Automation.TogglePattern]::Pattern)
    if ($null -ne $toggle) {{ $patterns.toggle = $toggle.Current.ToggleState.ToString() }}
    $expand = TryPattern $element ([System.Windows.Automation.ExpandCollapsePattern]::Pattern)
    if ($null -ne $expand) {{ $patterns.expandCollapse = $expand.Current.ExpandCollapseState.ToString() }}
    $selection = TryPattern $element ([System.Windows.Automation.SelectionItemPattern]::Pattern)
    if ($null -ne $selection) {{ $patterns.isSelected = [bool]$selection.Current.IsSelected }}
    return $patterns
}}

function ElementSummary($element) {{
    return [ordered]@{{
        runtimeId = RuntimeId $element
        name = [string]$element.Current.Name
        automationId = [string]$element.Current.AutomationId
        className = [string]$element.Current.ClassName
        controlType = ControlTypeName $element.Current.ControlType
        localizedControlType = [string]$element.Current.LocalizedControlType
        bounds = RectToObj $element.Current.BoundingRectangle
        state = StateInfo $element
    }}
}}

function IsMatch($element) {{
    if ($Criteria.runtimeId -and (RuntimeId $element) -ne [string]$Criteria.runtimeId) {{ return $false }}
    if ($Criteria.name -and [string]$element.Current.Name -ne [string]$Criteria.name) {{ return $false }}
    if ($Criteria.nameContains -and ([string]$element.Current.Name).IndexOf([string]$Criteria.nameContains, [System.StringComparison]::OrdinalIgnoreCase) -lt 0) {{ return $false }}
    if ($Criteria.automationId -and [string]$element.Current.AutomationId -ne [string]$Criteria.automationId) {{ return $false }}
    if ($Criteria.className -and [string]$element.Current.ClassName -ne [string]$Criteria.className) {{ return $false }}
    if ($Criteria.controlType -and (ControlTypeName $element.Current.ControlType) -ne [string]$Criteria.controlType) {{ return $false }}
    return $true
}}

function Walk($element, $depth) {{
    if ($script:Visited -ge $MaxNodes -or $null -eq $element) {{ return }}
    $script:Visited += 1
    if (IsMatch $element) {{ $script:Matches += $element }}
    if ($depth -ge $MaxDepth) {{ return }}

    $walker = [System.Windows.Automation.TreeWalker]::ControlViewWalker
    $child = $walker.GetFirstChild($element)
    while ($null -ne $child) {{
        Walk $child ($depth + 1)
        if ($script:Visited -ge $MaxNodes) {{ break }}
        $child = $walker.GetNextSibling($child)
    }}
}}

function FocusRoot() {{
    $focus = [System.Windows.Automation.AutomationElement]::FocusedElement
    if ($null -eq $focus) {{ return $null }}
    $walker = [System.Windows.Automation.TreeWalker]::ControlViewWalker
    $current = $focus
    $window = $null
    while ($null -ne $current) {{
        if ($current.Current.ControlType -eq [System.Windows.Automation.ControlType]::Window) {{
            $window = $current
        }}
        $current = $walker.GetParent($current)
    }}
    if ($null -eq $window) {{ $window = $focus }}
    return $window
}}

$root = FocusRoot
if ($null -eq $root) {{
    [ordered]@{{ ok = $false; error = 'No focused UI Automation element.' }} | ConvertTo-Json -Depth 8
    exit 0
}}

Walk $root 0
$index = 1
if ($Criteria.index) {{ $index = [int]$Criteria.index }}
if ($script:Matches.Count -lt $index) {{
    [ordered]@{{
        ok = $false
        error = 'No matching element.'
        matchCount = $script:Matches.Count
        visited = $script:Visited
    }} | ConvertTo-Json -Depth 8
    exit 0
}}

$element = $script:Matches[$index - 1]
$before = ElementSummary $element

switch ($Action) {{
    'toggle' {{
        $p = TryPattern $element ([System.Windows.Automation.TogglePattern]::Pattern)
        if ($null -eq $p) {{ throw 'Element does not expose TogglePattern.' }}
        $p.Toggle()
    }}
    'expand' {{
        $p = TryPattern $element ([System.Windows.Automation.ExpandCollapsePattern]::Pattern)
        if ($null -eq $p) {{ throw 'Element does not expose ExpandCollapsePattern.' }}
        $p.Expand()
    }}
    'collapse' {{
        $p = TryPattern $element ([System.Windows.Automation.ExpandCollapsePattern]::Pattern)
        if ($null -eq $p) {{ throw 'Element does not expose ExpandCollapsePattern.' }}
        $p.Collapse()
    }}
    'invoke' {{
        $p = TryPattern $element ([System.Windows.Automation.InvokePattern]::Pattern)
        if ($null -eq $p) {{ throw 'Element does not expose InvokePattern.' }}
        $p.Invoke()
    }}
    'select' {{
        $p = TryPattern $element ([System.Windows.Automation.SelectionItemPattern]::Pattern)
        if ($null -eq $p) {{ throw 'Element does not expose SelectionItemPattern.' }}
        $p.Select()
    }}
    default {{
        throw "Unsupported action '$Action'."
    }}
}}

Start-Sleep -Milliseconds 150
$after = ElementSummary $element
[ordered]@{{
    ok = $true
    action = $Action
    matchCount = $script:Matches.Count
    selectedIndex = $index
    before = $before
    after = $after
}} | ConvertTo-Json -Depth 40
"""


def treeview_script(
    *,
    window_title_contains: str | None,
    max_nodes: int,
) -> str:
    title_b64 = base64.b64encode((window_title_contains or "").encode("utf-8")).decode("ascii")
    script = r"""
$ErrorActionPreference = 'Stop'
$TitleBytes = [Convert]::FromBase64String('__TITLE_B64__')
$WindowTitleContains = [Text.Encoding]::UTF8.GetString($TitleBytes)
if ([string]::IsNullOrWhiteSpace($WindowTitleContains)) { $WindowTitleContains = $null }
$MaxNodes = __MAX_NODES__

try {
Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Text;

public static class Win32TreeViewReader
{
    const int TV_FIRST = 0x1100;
    const int TVM_GETNEXTITEM = TV_FIRST + 10;
    const int TVM_GETITEMRECT = TV_FIRST + 4;
    const int TVM_GETITEMW = TV_FIRST + 62;
    const int TVGN_ROOT = 0x0000;
    const int TVGN_NEXT = 0x0001;
    const int TVGN_CHILD = 0x0004;
    const uint TVIF_TEXT = 0x0001;
    const uint TVIF_STATE = 0x0008;
    const uint TVIS_STATEIMAGEMASK = 0xF000;
    const uint PROCESS_VM_OPERATION = 0x0008;
    const uint PROCESS_VM_READ = 0x0010;
    const uint PROCESS_VM_WRITE = 0x0020;
    const uint PROCESS_QUERY_LIMITED_INFORMATION = 0x1000;
    const uint MEM_COMMIT = 0x1000;
    const uint MEM_RESERVE = 0x2000;
    const uint MEM_RELEASE = 0x8000;
    const uint PAGE_READWRITE = 0x04;

    public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);

    [DllImport("user32.dll")]
    static extern bool EnumWindows(EnumWindowsProc lpEnumFunc, IntPtr lParam);
    [DllImport("user32.dll")]
    static extern bool EnumChildWindows(IntPtr hWndParent, EnumWindowsProc lpEnumFunc, IntPtr lParam);
    [DllImport("user32.dll")]
    static extern bool IsWindowVisible(IntPtr hWnd);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    static extern int GetWindowTextW(IntPtr hWnd, StringBuilder lpString, int nMaxCount);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    static extern int GetClassNameW(IntPtr hWnd, StringBuilder lpClassName, int nMaxCount);
    [DllImport("user32.dll")]
    static extern uint GetWindowThreadProcessId(IntPtr hWnd, out int processId);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    static extern IntPtr SendMessageW(IntPtr hWnd, int msg, IntPtr wParam, IntPtr lParam);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    static extern bool PostMessageW(IntPtr hWnd, int msg, IntPtr wParam, IntPtr lParam);
    [DllImport("user32.dll")]
    static extern bool ClientToScreen(IntPtr hWnd, ref POINT lpPoint);

    [DllImport("kernel32.dll", SetLastError = true)]
    static extern IntPtr OpenProcess(uint access, bool inheritHandle, int processId);
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool CloseHandle(IntPtr hObject);
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern IntPtr VirtualAllocEx(IntPtr hProcess, IntPtr lpAddress, UIntPtr dwSize, uint flAllocationType, uint flProtect);
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool VirtualFreeEx(IntPtr hProcess, IntPtr lpAddress, UIntPtr dwSize, uint dwFreeType);
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool WriteProcessMemory(IntPtr hProcess, IntPtr lpBaseAddress, byte[] lpBuffer, int nSize, out IntPtr bytesWritten);
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool ReadProcessMemory(IntPtr hProcess, IntPtr lpBaseAddress, byte[] lpBuffer, int nSize, out IntPtr bytesRead);
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool IsWow64Process(IntPtr hProcess, out bool wow64Process);

    [StructLayout(LayoutKind.Sequential)]
    public struct POINT { public int X; public int Y; }

    public class RectDump
    {
        public int left;
        public int top;
        public int right;
        public int bottom;
        public int width;
        public int height;
        public int centerX;
        public int centerY;
    }

    public class TreeItemDump
    {
        public int index;
        public int depth;
        public string handle;
        public string text;
        public int stateImageIndex;
        public string checkboxState;
        public RectDump bounds;
        public List<TreeItemDump> children = new List<TreeItemDump>();
    }

    public class TreeDump
    {
        public string hwnd;
        public string topHwnd;
        public string topTitle;
        public int processId;
        public int itemCount;
        public bool truncated;
        public string error;
        public List<TreeItemDump> items = new List<TreeItemDump>();
    }

    class TreeWindow
    {
        public IntPtr Hwnd;
        public IntPtr TopHwnd;
        public string TopTitle;
        public int ProcessId;
    }

    class RemoteBuffer : IDisposable
    {
        public IntPtr Process;
        public IntPtr Base;
        public IntPtr Item;
        public IntPtr Text;
        public IntPtr Rect;
        public int ItemSize;
        public int TextBytes;
        public int RectBytes;

        public RemoteBuffer(IntPtr process, bool target32Bit)
        {
            Process = process;
            ItemSize = target32Bit ? 40 : 56;
            TextBytes = 1024;
            RectBytes = target32Bit ? 16 : 24;
            int total = ItemSize + TextBytes + RectBytes;
            Base = VirtualAllocEx(process, IntPtr.Zero, new UIntPtr((uint)total), MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE);
            if (Base == IntPtr.Zero)
                throw new InvalidOperationException("VirtualAllocEx failed: " + Marshal.GetLastWin32Error());
            Item = Base;
            Text = Add(Base, ItemSize);
            Rect = Add(Text, TextBytes);
        }

        public void Dispose()
        {
            if (Base != IntPtr.Zero)
            {
                VirtualFreeEx(Process, Base, UIntPtr.Zero, MEM_RELEASE);
                Base = IntPtr.Zero;
            }
        }
    }

    public static object[] Dump(string titleContains, int maxNodes)
    {
        var trees = FindTreeViews(titleContains);
        var result = new List<object>();
        foreach (var tree in trees)
            result.Add(DumpTree(tree, maxNodes));
        return result.ToArray();
    }

    static List<TreeWindow> FindTreeViews(string titleContains)
    {
        var trees = new List<TreeWindow>();
        EnumWindows(delegate(IntPtr top, IntPtr lp)
        {
            if (!IsWindowVisible(top))
                return true;
            string title = WindowText(top);
            if (!String.IsNullOrEmpty(titleContains) &&
                (title == null || title.IndexOf(titleContains, StringComparison.OrdinalIgnoreCase) < 0))
                return true;

            EnumChildWindows(top, delegate(IntPtr child, IntPtr childLp)
            {
                if (ClassName(child).Equals("SysTreeView32", StringComparison.OrdinalIgnoreCase))
                {
                    int pid;
                    GetWindowThreadProcessId(child, out pid);
                    trees.Add(new TreeWindow {
                        Hwnd = child,
                        TopHwnd = top,
                        TopTitle = title,
                        ProcessId = pid
                    });
                }
                return true;
            }, IntPtr.Zero);
            return true;
        }, IntPtr.Zero);
        return trees;
    }

    static TreeDump DumpTree(TreeWindow tree, int maxNodes)
    {
        var dump = new TreeDump {
            hwnd = PtrString(tree.Hwnd),
            topHwnd = PtrString(tree.TopHwnd),
            topTitle = tree.TopTitle,
            processId = tree.ProcessId
        };

        IntPtr process = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_VM_OPERATION | PROCESS_VM_READ | PROCESS_VM_WRITE, false, tree.ProcessId);
        if (process == IntPtr.Zero)
        {
            dump.error = "OpenProcess failed: " + Marshal.GetLastWin32Error();
            return dump;
        }

        try
        {
            bool target32Bit = IsTarget32Bit(process);
            using (var buffer = new RemoteBuffer(process, target32Bit))
            {
                IntPtr root = SendMessageW(tree.Hwnd, TVM_GETNEXTITEM, new IntPtr(TVGN_ROOT), IntPtr.Zero);
                int[] count = new int[] { 0 };
                while (root != IntPtr.Zero)
                {
                    if (count[0] >= maxNodes) { dump.truncated = true; break; }
                    var item = ReadItem(tree.Hwnd, process, buffer, root, target32Bit, 0, count, maxNodes, ref dump.truncated);
                    if (item != null) dump.items.Add(item);
                    root = SendMessageW(tree.Hwnd, TVM_GETNEXTITEM, new IntPtr(TVGN_NEXT), root);
                }
                dump.itemCount = count[0];
            }
        }
        catch (Exception e)
        {
            dump.error = e.Message;
        }
        finally
        {
            CloseHandle(process);
        }

        return dump;
    }

    static TreeItemDump ReadItem(
        IntPtr hwnd,
        IntPtr process,
        RemoteBuffer buffer,
        IntPtr hItem,
        bool target32Bit,
        int depth,
        int[] count,
        int maxNodes,
        ref bool truncated)
    {
        if (count[0] >= maxNodes) { truncated = true; return null; }
        count[0]++;

        uint state;
        string text = GetItemTextAndState(hwnd, process, buffer, hItem, target32Bit, out state);
        int stateImageIndex = (int)((state & TVIS_STATEIMAGEMASK) >> 12);
        var item = new TreeItemDump {
            index = count[0],
            depth = depth,
            handle = PtrString(hItem),
            text = text,
            stateImageIndex = stateImageIndex,
            checkboxState = CheckboxState(stateImageIndex),
            bounds = GetItemRect(hwnd, process, buffer, hItem, target32Bit)
        };

        IntPtr child = SendMessageW(hwnd, TVM_GETNEXTITEM, new IntPtr(TVGN_CHILD), hItem);
        while (child != IntPtr.Zero)
        {
            if (count[0] >= maxNodes) { truncated = true; break; }
            var childDump = ReadItem(hwnd, process, buffer, child, target32Bit, depth + 1, count, maxNodes, ref truncated);
            if (childDump != null) item.children.Add(childDump);
            child = SendMessageW(hwnd, TVM_GETNEXTITEM, new IntPtr(TVGN_NEXT), child);
        }
        return item;
    }

    static string GetItemTextAndState(IntPtr hwnd, IntPtr process, RemoteBuffer buffer, IntPtr hItem, bool target32Bit, out uint state)
    {
        byte[] tvitem = new byte[buffer.ItemSize];
        PutUInt32(tvitem, 0, TVIF_TEXT | TVIF_STATE);
        PutPtr(tvitem, target32Bit ? 4 : 8, hItem, target32Bit);
        PutUInt32(tvitem, target32Bit ? 12 : 20, TVIS_STATEIMAGEMASK);
        PutPtr(tvitem, target32Bit ? 16 : 24, buffer.Text, target32Bit);
        PutInt32(tvitem, target32Bit ? 20 : 32, buffer.TextBytes / 2);
        Write(process, buffer.Item, tvitem);

        IntPtr ok = SendMessageW(hwnd, TVM_GETITEMW, IntPtr.Zero, buffer.Item);
        if (ok == IntPtr.Zero)
        {
            state = 0;
            return "";
        }

        byte[] itemBack = Read(process, buffer.Item, buffer.ItemSize);
        state = BitConverter.ToUInt32(itemBack, target32Bit ? 8 : 16);
        byte[] textBack = Read(process, buffer.Text, buffer.TextBytes);
        return DecodeNullTerminatedUnicode(textBack);
    }

    static RectDump GetItemRect(IntPtr hwnd, IntPtr process, RemoteBuffer buffer, IntPtr hItem, bool target32Bit)
    {
        byte[] rect = new byte[buffer.RectBytes];
        PutPtr(rect, 0, hItem, target32Bit);
        Write(process, buffer.Rect, rect);
        IntPtr ok = SendMessageW(hwnd, TVM_GETITEMRECT, IntPtr.Zero, buffer.Rect);
        if (ok == IntPtr.Zero)
            return null;

        byte[] rectBack = Read(process, buffer.Rect, 16);
        int left = BitConverter.ToInt32(rectBack, 0);
        int top = BitConverter.ToInt32(rectBack, 4);
        int right = BitConverter.ToInt32(rectBack, 8);
        int bottom = BitConverter.ToInt32(rectBack, 12);
        POINT p1 = new POINT { X = left, Y = top };
        POINT p2 = new POINT { X = right, Y = bottom };
        ClientToScreen(hwnd, ref p1);
        ClientToScreen(hwnd, ref p2);

        return new RectDump {
            left = p1.X,
            top = p1.Y,
            right = p2.X,
            bottom = p2.Y,
            width = p2.X - p1.X,
            height = p2.Y - p1.Y,
            centerX = (p1.X + p2.X) / 2,
            centerY = (p1.Y + p2.Y) / 2
        };
    }

    static bool IsTarget32Bit(IntPtr process)
    {
        if (IntPtr.Size == 4)
            return true;
        bool wow64;
        if (IsWow64Process(process, out wow64))
            return wow64;
        return false;
    }

    static string CheckboxState(int stateImageIndex)
    {
        switch (stateImageIndex)
        {
            case 0: return "none";
            case 1: return "unchecked";
            case 2: return "checked";
            case 3: return "mixed";
            default: return "stateImage" + stateImageIndex.ToString();
        }
    }

    static string WindowText(IntPtr hwnd)
    {
        var sb = new StringBuilder(512);
        GetWindowTextW(hwnd, sb, sb.Capacity);
        return sb.ToString();
    }

    static string ClassName(IntPtr hwnd)
    {
        var sb = new StringBuilder(256);
        GetClassNameW(hwnd, sb, sb.Capacity);
        return sb.ToString();
    }

    static string PtrString(IntPtr ptr)
    {
        return "0x" + ptr.ToInt64().ToString("X");
    }

    static IntPtr Add(IntPtr ptr, int offset)
    {
        return new IntPtr(ptr.ToInt64() + offset);
    }

    static void PutUInt32(byte[] buffer, int offset, uint value)
    {
        Array.Copy(BitConverter.GetBytes(value), 0, buffer, offset, 4);
    }

    static void PutInt32(byte[] buffer, int offset, int value)
    {
        Array.Copy(BitConverter.GetBytes(value), 0, buffer, offset, 4);
    }

    static void PutPtr(byte[] buffer, int offset, IntPtr value, bool target32Bit)
    {
        long raw = value.ToInt64();
        if (target32Bit)
            Array.Copy(BitConverter.GetBytes((uint)raw), 0, buffer, offset, 4);
        else
            Array.Copy(BitConverter.GetBytes(raw), 0, buffer, offset, 8);
    }

    static void Write(IntPtr process, IntPtr address, byte[] data)
    {
        IntPtr written;
        if (!WriteProcessMemory(process, address, data, data.Length, out written))
            throw new InvalidOperationException("WriteProcessMemory failed: " + Marshal.GetLastWin32Error());
    }

    static byte[] Read(IntPtr process, IntPtr address, int length)
    {
        byte[] data = new byte[length];
        IntPtr read;
        if (!ReadProcessMemory(process, address, data, data.Length, out read))
            throw new InvalidOperationException("ReadProcessMemory failed: " + Marshal.GetLastWin32Error());
        return data;
    }

    static string DecodeNullTerminatedUnicode(byte[] data)
    {
        int length = 0;
        while (length + 1 < data.Length)
        {
            if (data[length] == 0 && data[length + 1] == 0)
                break;
            length += 2;
        }
        return Encoding.Unicode.GetString(data, 0, length);
    }
}
'@

$treeviews = [Win32TreeViewReader]::Dump($WindowTitleContains, $MaxNodes)
[ordered]@{
    ok = $true
    source = 'Win32 SysTreeView32 TVM_GETITEM TVIS_STATEIMAGEMASK'
    note = 'checkboxState is inferred from the TreeView state image index: 1=unchecked, 2=checked, 3=mixed. Bounds are TreeView item rectangles in screen coordinates when available.'
    windowTitleContains = $WindowTitleContains
    treeviews = $treeviews
} | ConvertTo-Json -Depth 80
} catch {
    [ordered]@{
        ok = $false
        error = $_.Exception.Message
        detail = $_.ScriptStackTrace
    } | ConvertTo-Json -Depth 20
}
"""
    return (
        script
        .replace("__TITLE_B64__", title_b64)
        .replace("__MAX_NODES__", str(max_nodes))
    )


def treeview_action_script(
    *,
    action: str,
    window_title_contains: str | None,
    text: str | None,
    text_contains: str | None,
    handle: str | None,
    index: int,
    max_nodes: int,
    click_method: str,
) -> str:
    criteria = {
        "action": action,
        "windowTitleContains": window_title_contains or "",
        "text": text or "",
        "textContains": text_contains or "",
        "handle": handle or "",
        "index": index,
        "maxNodes": max_nodes,
        "clickMethod": click_method,
    }
    criteria_json = json.dumps(criteria, separators=(",", ":"))
    criteria_b64 = base64.b64encode(criteria_json.encode("utf-8")).decode("ascii")
    return rf"""
$ErrorActionPreference = 'Stop'
$CriteriaJson = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{criteria_b64}'))
$Criteria = $CriteriaJson | ConvertFrom-Json

try {{
Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;

public static class Win32TreeViewAction
{{
    const int TV_FIRST = 0x1100;
    const int TVM_GETNEXTITEM = TV_FIRST + 10;
    const int TVM_GETITEMRECT = TV_FIRST + 4;
    const int TVM_GETITEMW = TV_FIRST + 62;
    const int TVM_HITTEST = TV_FIRST + 17;
    const int TVM_EXPAND = TV_FIRST + 2;
    const int TVM_SELECTITEM = TV_FIRST + 11;
    const int TVGN_ROOT = 0x0000;
    const int TVGN_NEXT = 0x0001;
    const int TVGN_CHILD = 0x0004;
    const int TVGN_CARET = 0x0009;
    const int TVE_COLLAPSE = 0x0001;
    const int TVE_EXPAND = 0x0002;
    const int WM_KEYDOWN = 0x0100;
    const int WM_KEYUP = 0x0101;
    const uint TVIF_TEXT = 0x0001;
    const uint TVIF_STATE = 0x0008;
    const uint TVIS_STATEIMAGEMASK = 0xF000;
    const uint TVHT_ONITEMSTATEICON = 0x0040;
    const int WM_MOUSEMOVE = 0x0200;
    const int WM_LBUTTONDOWN = 0x0201;
    const int WM_LBUTTONUP = 0x0202;
    const int MK_LBUTTON = 0x0001;
    const uint PROCESS_VM_OPERATION = 0x0008;
    const uint PROCESS_VM_READ = 0x0010;
    const uint PROCESS_VM_WRITE = 0x0020;
    const uint PROCESS_QUERY_LIMITED_INFORMATION = 0x1000;
    const uint MEM_COMMIT = 0x1000;
    const uint MEM_RESERVE = 0x2000;
    const uint MEM_RELEASE = 0x8000;
    const uint PAGE_READWRITE = 0x04;
    const uint INPUT_MOUSE = 0;
    const uint MOUSEEVENTF_LEFTDOWN = 0x0002;
    const uint MOUSEEVENTF_LEFTUP = 0x0004;
    const int SW_RESTORE = 9;
    const int VK_SPACE = 0x20;

    public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);

    [DllImport("user32.dll")]
    static extern bool EnumWindows(EnumWindowsProc lpEnumFunc, IntPtr lParam);
    [DllImport("user32.dll")]
    static extern bool EnumChildWindows(IntPtr hWndParent, EnumWindowsProc lpEnumFunc, IntPtr lParam);
    [DllImport("user32.dll")]
    static extern bool IsWindowVisible(IntPtr hWnd);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    static extern int GetWindowTextW(IntPtr hWnd, StringBuilder lpString, int nMaxCount);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    static extern int GetClassNameW(IntPtr hWnd, StringBuilder lpClassName, int nMaxCount);
    [DllImport("user32.dll")]
    static extern uint GetWindowThreadProcessId(IntPtr hWnd, out int processId);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    static extern IntPtr SendMessageW(IntPtr hWnd, int msg, IntPtr wParam, IntPtr lParam);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    static extern bool PostMessageW(IntPtr hWnd, int msg, IntPtr wParam, IntPtr lParam);
    [DllImport("user32.dll")]
    static extern bool ClientToScreen(IntPtr hWnd, ref POINT lpPoint);
    [DllImport("user32.dll")]
    static extern bool ScreenToClient(IntPtr hWnd, ref POINT lpPoint);
    [DllImport("user32.dll")]
    static extern bool GetClientRect(IntPtr hWnd, out RECT rect);
    [DllImport("user32.dll")]
    static extern IntPtr SetFocus(IntPtr hWnd);
    [DllImport("user32.dll")]
    static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")]
    static extern bool BringWindowToTop(IntPtr hWnd);
    [DllImport("user32.dll")]
    static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
    [DllImport("user32.dll")]
    static extern IntPtr SetActiveWindow(IntPtr hWnd);
    [DllImport("user32.dll")]
    static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")]
    static extern bool AttachThreadInput(uint idAttach, uint idAttachTo, bool fAttach);
    [DllImport("user32.dll", SetLastError = true)]
    static extern bool SetCursorPos(int X, int Y);
    [DllImport("user32.dll")]
    static extern bool GetCursorPos(out POINT lpPoint);
    [DllImport("user32.dll", SetLastError = true)]
    static extern uint SendInput(uint nInputs, INPUT[] pInputs, int cbSize);

    [DllImport("kernel32.dll", SetLastError = true)]
    static extern IntPtr OpenProcess(uint access, bool inheritHandle, int processId);
    [DllImport("kernel32.dll")]
    static extern uint GetCurrentThreadId();
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool CloseHandle(IntPtr hObject);
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern IntPtr VirtualAllocEx(IntPtr hProcess, IntPtr lpAddress, UIntPtr dwSize, uint flAllocationType, uint flProtect);
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool VirtualFreeEx(IntPtr hProcess, IntPtr lpAddress, UIntPtr dwSize, uint dwFreeType);
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool WriteProcessMemory(IntPtr hProcess, IntPtr lpBaseAddress, byte[] lpBuffer, int nSize, out IntPtr bytesWritten);
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool ReadProcessMemory(IntPtr hProcess, IntPtr lpBaseAddress, byte[] lpBuffer, int nSize, out IntPtr bytesRead);
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool IsWow64Process(IntPtr hProcess, out bool wow64Process);

    [StructLayout(LayoutKind.Sequential)]
    public struct POINT {{ public int X; public int Y; }}
    [StructLayout(LayoutKind.Sequential)]
    public struct RECT {{ public int Left; public int Top; public int Right; public int Bottom; }}
    [StructLayout(LayoutKind.Sequential)]
    public struct INPUT {{ public uint type; public MOUSEINPUT mi; }}
    [StructLayout(LayoutKind.Sequential)]
    public struct MOUSEINPUT
    {{
        public int dx;
        public int dy;
        public uint mouseData;
        public uint dwFlags;
        public uint time;
        public UIntPtr dwExtraInfo;
    }}

    public class RectDump
    {{
        public int left;
        public int top;
        public int right;
        public int bottom;
        public int width;
        public int height;
        public int centerX;
        public int centerY;
    }}

    public class ItemState
    {{
        public int index;
        public int depth;
        public string handle;
        public string text;
        public int stateImageIndex;
        public string checkboxState;
        public RectDump bounds;
    }}

    public class HitInfo
    {{
        public bool found;
        public int clientX;
        public int clientY;
        public int screenX;
        public int screenY;
        public uint flags;
        public string hItem;
    }}

    public class FocusResult
    {{
        public string beforeForeground;
        public string afterForeground;
        public uint currentThread;
        public uint targetThread;
        public uint foregroundThread;
        public bool attachedTarget;
        public bool attachedForeground;
    }}

    public class ClickResult
    {{
        public string method;
        public int screenX;
        public int screenY;
        public int clientX;
        public int clientY;
        public FocusResult focus;
        public bool setCursorPosOk;
        public int setCursorPosError;
        public int cursorBeforeX;
        public int cursorBeforeY;
        public int cursorAfterX;
        public int cursorAfterY;
        public int sentInputs;
        public int sendInputError;
        public bool postedMessages;
    }}

    class TreeWindow
    {{
        public IntPtr Hwnd;
        public IntPtr TopHwnd;
        public string TopTitle;
        public int ProcessId;
    }}

    class Match
    {{
        public TreeWindow Tree;
        public IntPtr HItem;
        public int Index;
        public int Depth;
    }}

    class RemoteBuffer : IDisposable
    {{
        public IntPtr Process;
        public IntPtr Base;
        public IntPtr Item;
        public IntPtr Text;
        public IntPtr Rect;
        public IntPtr Hit;
        public int ItemSize;
        public int TextBytes;
        public int RectBytes;
        public int HitBytes;

        public RemoteBuffer(IntPtr process, bool target32Bit)
        {{
            Process = process;
            ItemSize = target32Bit ? 40 : 56;
            TextBytes = 1024;
            RectBytes = target32Bit ? 16 : 24;
            HitBytes = target32Bit ? 16 : 24;
            int total = ItemSize + TextBytes + RectBytes + HitBytes;
            Base = VirtualAllocEx(process, IntPtr.Zero, new UIntPtr((uint)total), MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE);
            if (Base == IntPtr.Zero)
                throw new InvalidOperationException("VirtualAllocEx failed: " + Marshal.GetLastWin32Error());
            Item = Base;
            Text = Add(Item, ItemSize);
            Rect = Add(Text, TextBytes);
            Hit = Add(Rect, RectBytes);
        }}

        public void Dispose()
        {{
            if (Base != IntPtr.Zero)
            {{
                VirtualFreeEx(Process, Base, UIntPtr.Zero, MEM_RELEASE);
                Base = IntPtr.Zero;
            }}
        }}
    }}

    public static object Act(
        string action,
        string titleContains,
        string text,
        string textContains,
        string handle,
        int requestedIndex,
        int maxNodes,
        string clickMethod)
    {{
        action = (action ?? "").ToLowerInvariant();
        clickMethod = String.IsNullOrEmpty(clickMethod) ? "keyboard" : clickMethod.ToLowerInvariant();
        var matches = FindMatches(titleContains, text, textContains, handle, maxNodes);
        int index = requestedIndex < 1 ? 1 : requestedIndex;
        if (matches.Count < index)
            return new {{ ok = false, error = "No matching TreeView item.", matchCount = matches.Count }};

        var match = matches[index - 1];
        IntPtr process = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_VM_OPERATION | PROCESS_VM_READ | PROCESS_VM_WRITE, false, match.Tree.ProcessId);
        if (process == IntPtr.Zero)
            return new {{ ok = false, error = "OpenProcess failed: " + Marshal.GetLastWin32Error(), matchCount = matches.Count }};

        try
        {{
            bool target32Bit = IsTarget32Bit(process);
            using (var buffer = new RemoteBuffer(process, target32Bit))
            {{
                ItemState before = ReadItemState(match.Tree.Hwnd, process, buffer, match.HItem, target32Bit, match.Index, match.Depth);
                HitInfo hit = null;
                ClickResult click = null;
                ItemState afterMouse = null;
                bool attempted = false;
                bool fallbackAttempted = false;
                string note = null;
                string effectiveClickMethod = clickMethod;

                if (action == "check" && before.checkboxState == "checked")
                {{
                    note = "already checked";
                }}
                else if (action == "uncheck" && before.checkboxState == "unchecked")
                {{
                    note = "already unchecked";
                }}
                else if (action == "expand" || action == "collapse")
                {{
                    attempted = true;
                    int code = action == "expand" ? TVE_EXPAND : TVE_COLLAPSE;
                    SendMessageW(match.Tree.Hwnd, TVM_EXPAND, new IntPtr(code), match.HItem);
                    Thread.Sleep(150);
                }}
                else if (action == "toggle" || action == "check" || action == "uncheck")
                {{
                    attempted = true;
                    if (clickMethod == "keyboard")
                    {{
                        KeyboardToggle(match.Tree, match.HItem);
                    }}
                    else
                    {{
                        string mouseMethod = clickMethod == "auto" ? "input" : clickMethod;
                        effectiveClickMethod = mouseMethod;
                        hit = FindStateIconHit(match.Tree.Hwnd, process, buffer, match.HItem, target32Bit);
                        if (hit == null || !hit.found)
                        {{
                            if (clickMethod != "auto")
                                return new {{ ok = false, error = "Could not find TVHT_ONITEMSTATEICON for target item.", matchCount = matches.Count, before = before }};
                            fallbackAttempted = true;
                            effectiveClickMethod = "keyboard";
                            KeyboardToggle(match.Tree, match.HItem);
                        }}
                        else
                        {{
                            click = ClickStateIcon(match.Tree, hit, mouseMethod);
                            Thread.Sleep(250);
                            afterMouse = ReadItemState(match.Tree.Hwnd, process, buffer, match.HItem, target32Bit, match.Index, match.Depth);
                            if (!DesiredStateReached(action, before, afterMouse))
                            {{
                                fallbackAttempted = true;
                                effectiveClickMethod = mouseMethod + "+keyboard";
                                KeyboardToggle(match.Tree, match.HItem);
                            }}
                        }}
                    }}
                    Thread.Sleep(250);
                }}
                else
                {{
                    return new {{ ok = false, error = "Unsupported action. Use toggle, check, uncheck, expand, or collapse." }};
                }}

                ItemState after = ReadItemState(match.Tree.Hwnd, process, buffer, match.HItem, target32Bit, match.Index, match.Depth);
                bool desiredOk = DesiredStateReached(action, before, after);

                return new {{
                    ok = desiredOk,
                    action = action,
                    attempted = attempted,
                    note = note,
                    clickMethod = clickMethod,
                    effectiveClickMethod = effectiveClickMethod,
                    fallbackAttempted = fallbackAttempted,
                    matchCount = matches.Count,
                    selectedIndex = index,
                    tree = new {{
                        hwnd = PtrString(match.Tree.Hwnd),
                        topTitle = match.Tree.TopTitle,
                        processId = match.Tree.ProcessId
                    }},
                    hit = hit,
                    click = click,
                    before = before,
                    afterMouse = afterMouse,
                    after = after
                }};
            }}
        }}
        finally
        {{
            CloseHandle(process);
        }}
    }}

    static List<Match> FindMatches(string titleContains, string text, string textContains, string handle, int maxNodes)
    {{
        var matches = new List<Match>();
        foreach (var tree in FindTreeViews(titleContains))
        {{
            int pid = tree.ProcessId;
            IntPtr process = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_VM_OPERATION | PROCESS_VM_READ | PROCESS_VM_WRITE, false, pid);
            if (process == IntPtr.Zero)
                continue;
            try
            {{
                bool target32Bit = IsTarget32Bit(process);
                using (var buffer = new RemoteBuffer(process, target32Bit))
                {{
                    IntPtr root = SendMessageW(tree.Hwnd, TVM_GETNEXTITEM, new IntPtr(TVGN_ROOT), IntPtr.Zero);
                    int[] count = new int[] {{ 0 }};
                    while (root != IntPtr.Zero && count[0] < maxNodes)
                    {{
                        Walk(tree, process, buffer, root, target32Bit, 0, count, maxNodes, text, textContains, handle, matches);
                        root = SendMessageW(tree.Hwnd, TVM_GETNEXTITEM, new IntPtr(TVGN_NEXT), root);
                    }}
                }}
            }}
            finally
            {{
                CloseHandle(process);
            }}
        }}
        return matches;
    }}

    static void Walk(TreeWindow tree, IntPtr process, RemoteBuffer buffer, IntPtr hItem, bool target32Bit, int depth, int[] count, int maxNodes, string text, string textContains, string handle, List<Match> matches)
    {{
        if (hItem == IntPtr.Zero || count[0] >= maxNodes) return;
        count[0]++;
        ItemState state = ReadItemState(tree.Hwnd, process, buffer, hItem, target32Bit, count[0], depth);
        bool matched = true;
        if (!String.IsNullOrEmpty(handle) && !String.Equals(state.handle, handle, StringComparison.OrdinalIgnoreCase)) matched = false;
        if (!String.IsNullOrEmpty(text) && !String.Equals(state.text, text, StringComparison.Ordinal)) matched = false;
        if (!String.IsNullOrEmpty(textContains) && (state.text == null || state.text.IndexOf(textContains, StringComparison.OrdinalIgnoreCase) < 0)) matched = false;
        if (matched)
            matches.Add(new Match {{ Tree = tree, HItem = hItem, Index = state.index, Depth = depth }});

        IntPtr child = SendMessageW(tree.Hwnd, TVM_GETNEXTITEM, new IntPtr(TVGN_CHILD), hItem);
        while (child != IntPtr.Zero && count[0] < maxNodes)
        {{
            Walk(tree, process, buffer, child, target32Bit, depth + 1, count, maxNodes, text, textContains, handle, matches);
            child = SendMessageW(tree.Hwnd, TVM_GETNEXTITEM, new IntPtr(TVGN_NEXT), child);
        }}
    }}

    static List<TreeWindow> FindTreeViews(string titleContains)
    {{
        var trees = new List<TreeWindow>();
        EnumWindows(delegate(IntPtr top, IntPtr lp)
        {{
            if (!IsWindowVisible(top))
                return true;
            string title = WindowText(top);
            if (!String.IsNullOrEmpty(titleContains) &&
                (title == null || title.IndexOf(titleContains, StringComparison.OrdinalIgnoreCase) < 0))
                return true;

            EnumChildWindows(top, delegate(IntPtr child, IntPtr childLp)
            {{
                if (ClassName(child).Equals("SysTreeView32", StringComparison.OrdinalIgnoreCase))
                {{
                    int pid;
                    GetWindowThreadProcessId(child, out pid);
                    trees.Add(new TreeWindow {{ Hwnd = child, TopHwnd = top, TopTitle = title, ProcessId = pid }});
                }}
                return true;
            }}, IntPtr.Zero);
            return true;
        }}, IntPtr.Zero);
        return trees;
    }}

    static ItemState ReadItemState(IntPtr hwnd, IntPtr process, RemoteBuffer buffer, IntPtr hItem, bool target32Bit, int index, int depth)
    {{
        uint state;
        string text = GetItemTextAndState(hwnd, process, buffer, hItem, target32Bit, out state);
        int stateImageIndex = (int)((state & TVIS_STATEIMAGEMASK) >> 12);
        return new ItemState {{
            index = index,
            depth = depth,
            handle = PtrString(hItem),
            text = text,
            stateImageIndex = stateImageIndex,
            checkboxState = CheckboxState(stateImageIndex),
            bounds = GetItemRect(hwnd, process, buffer, hItem, target32Bit)
        }};
    }}

    static string GetItemTextAndState(IntPtr hwnd, IntPtr process, RemoteBuffer buffer, IntPtr hItem, bool target32Bit, out uint state)
    {{
        byte[] tvitem = new byte[buffer.ItemSize];
        PutUInt32(tvitem, 0, TVIF_TEXT | TVIF_STATE);
        PutPtr(tvitem, target32Bit ? 4 : 8, hItem, target32Bit);
        PutUInt32(tvitem, target32Bit ? 12 : 20, TVIS_STATEIMAGEMASK);
        PutPtr(tvitem, target32Bit ? 16 : 24, buffer.Text, target32Bit);
        PutInt32(tvitem, target32Bit ? 20 : 32, buffer.TextBytes / 2);
        Write(process, buffer.Item, tvitem);

        IntPtr ok = SendMessageW(hwnd, TVM_GETITEMW, IntPtr.Zero, buffer.Item);
        if (ok == IntPtr.Zero)
        {{
            state = 0;
            return "";
        }}

        byte[] itemBack = Read(process, buffer.Item, buffer.ItemSize);
        state = BitConverter.ToUInt32(itemBack, target32Bit ? 8 : 16);
        byte[] textBack = Read(process, buffer.Text, buffer.TextBytes);
        return DecodeNullTerminatedUnicode(textBack);
    }}

    static RectDump GetItemRect(IntPtr hwnd, IntPtr process, RemoteBuffer buffer, IntPtr hItem, bool target32Bit)
    {{
        byte[] rect = new byte[buffer.RectBytes];
        PutPtr(rect, 0, hItem, target32Bit);
        Write(process, buffer.Rect, rect);
        IntPtr ok = SendMessageW(hwnd, TVM_GETITEMRECT, IntPtr.Zero, buffer.Rect);
        if (ok == IntPtr.Zero)
            return null;

        byte[] rectBack = Read(process, buffer.Rect, 16);
        int left = BitConverter.ToInt32(rectBack, 0);
        int top = BitConverter.ToInt32(rectBack, 4);
        int right = BitConverter.ToInt32(rectBack, 8);
        int bottom = BitConverter.ToInt32(rectBack, 12);
        POINT p1 = new POINT {{ X = left, Y = top }};
        POINT p2 = new POINT {{ X = right, Y = bottom }};
        ClientToScreen(hwnd, ref p1);
        ClientToScreen(hwnd, ref p2);

        return new RectDump {{
            left = p1.X,
            top = p1.Y,
            right = p2.X,
            bottom = p2.Y,
            width = p2.X - p1.X,
            height = p2.Y - p1.Y,
            centerX = (p1.X + p2.X) / 2,
            centerY = (p1.Y + p2.Y) / 2
        }};
    }}

    static HitInfo FindStateIconHit(IntPtr hwnd, IntPtr process, RemoteBuffer buffer, IntPtr hItem, bool target32Bit)
    {{
        RectDump bounds = GetItemRect(hwnd, process, buffer, hItem, target32Bit);
        RECT client;
        GetClientRect(hwnd, out client);
        int y = 0;
        if (bounds != null)
        {{
            POINT p = new POINT {{ X = bounds.centerX, Y = bounds.centerY }};
            ScreenToClient(hwnd, ref p);
            y = p.Y;
        }}
        else
        {{
            y = Math.Max(0, (client.Bottom - client.Top) / 2);
        }}

        int width = Math.Max(0, client.Right - client.Left);
        int firstX = -1;
        int lastX = -1;
        uint foundFlags = 0;
        IntPtr foundItem = IntPtr.Zero;
        for (int x = 0; x < width; x++)
        {{
            byte[] hit = new byte[buffer.HitBytes];
            PutInt32(hit, 0, x);
            PutInt32(hit, 4, y);
            Write(process, buffer.Hit, hit);
            SendMessageW(hwnd, TVM_HITTEST, IntPtr.Zero, buffer.Hit);
            byte[] back = Read(process, buffer.Hit, buffer.HitBytes);
            uint flags = BitConverter.ToUInt32(back, 8);
            IntPtr found = GetPtr(back, target32Bit ? 12 : 16, target32Bit);
            if (found == hItem && (flags & TVHT_ONITEMSTATEICON) != 0)
            {{
                if (firstX < 0) firstX = x;
                lastX = x;
                foundFlags = flags;
                foundItem = found;
            }}
            else if (firstX >= 0)
            {{
                break;
            }}
        }}
        if (firstX >= 0)
        {{
            int clickX = (firstX + lastX) / 2;
            POINT screen = new POINT {{ X = clickX, Y = y }};
            ClientToScreen(hwnd, ref screen);
            return new HitInfo {{
                found = true,
                clientX = clickX,
                clientY = y,
                screenX = screen.X,
                screenY = screen.Y,
                flags = foundFlags,
                hItem = PtrString(foundItem)
            }};
        }}
        return new HitInfo {{ found = false, clientY = y }};
    }}

    static ClickResult ClickStateIcon(TreeWindow tree, HitInfo hit, string method)
    {{
        var result = new ClickResult {{
            method = method,
            screenX = hit.screenX,
            screenY = hit.screenY,
            clientX = hit.clientX,
            clientY = hit.clientY,
            focus = FocusTree(tree)
        }};

        if (method == "input")
        {{
            POINT before;
            if (GetCursorPos(out before))
            {{
                result.cursorBeforeX = before.X;
                result.cursorBeforeY = before.Y;
            }}

            result.setCursorPosOk = SetCursorPos(hit.screenX, hit.screenY);
            if (!result.setCursorPosOk)
                result.setCursorPosError = Marshal.GetLastWin32Error();

            Thread.Sleep(50);

            var inputs = new INPUT[2];
            inputs[0].type = INPUT_MOUSE;
            inputs[0].mi.dwFlags = MOUSEEVENTF_LEFTDOWN;
            inputs[1].type = INPUT_MOUSE;
            inputs[1].mi.dwFlags = MOUSEEVENTF_LEFTUP;
            uint sent = SendInput(2, inputs, Marshal.SizeOf(typeof(INPUT)));
            result.sentInputs = (int)sent;
            if (sent != 2)
                result.sendInputError = Marshal.GetLastWin32Error();

            POINT after;
            if (GetCursorPos(out after))
            {{
                result.cursorAfterX = after.X;
                result.cursorAfterY = after.Y;
            }}
        }}
        else
        {{
            IntPtr lp = MakeLParam(hit.clientX, hit.clientY);
            PostMessageW(tree.Hwnd, WM_MOUSEMOVE, IntPtr.Zero, lp);
            PostMessageW(tree.Hwnd, WM_LBUTTONDOWN, new IntPtr(MK_LBUTTON), lp);
            PostMessageW(tree.Hwnd, WM_LBUTTONUP, IntPtr.Zero, lp);
            result.postedMessages = true;
        }}
        return result;
    }}

    static void KeyboardToggle(TreeWindow tree, IntPtr hItem)
    {{
        FocusTree(tree);
        SendMessageW(tree.Hwnd, TVM_SELECTITEM, new IntPtr(TVGN_CARET), hItem);
        Thread.Sleep(50);
        PostMessageW(tree.Hwnd, WM_KEYDOWN, new IntPtr(VK_SPACE), IntPtr.Zero);
        PostMessageW(tree.Hwnd, WM_KEYUP, new IntPtr(VK_SPACE), IntPtr.Zero);
    }}

    static FocusResult FocusTree(TreeWindow tree)
    {{
        var result = new FocusResult();
        IntPtr before = GetForegroundWindow();
        result.beforeForeground = PtrString(before);

        int ignored;
        uint currentThread = GetCurrentThreadId();
        uint targetThread = GetWindowThreadProcessId(tree.Hwnd, out ignored);
        uint foregroundThread = before == IntPtr.Zero ? 0 : GetWindowThreadProcessId(before, out ignored);
        result.currentThread = currentThread;
        result.targetThread = targetThread;
        result.foregroundThread = foregroundThread;

        bool attachTarget = targetThread != 0 && targetThread != currentThread;
        bool attachForeground = foregroundThread != 0 && foregroundThread != currentThread && foregroundThread != targetThread;
        if (attachTarget)
            result.attachedTarget = AttachThreadInput(currentThread, targetThread, true);
        if (attachForeground)
            result.attachedForeground = AttachThreadInput(currentThread, foregroundThread, true);

        try
        {{
            ShowWindow(tree.TopHwnd, SW_RESTORE);
            BringWindowToTop(tree.TopHwnd);
            SetForegroundWindow(tree.TopHwnd);
            SetActiveWindow(tree.TopHwnd);
            SetFocus(tree.Hwnd);
            Thread.Sleep(80);
            result.afterForeground = PtrString(GetForegroundWindow());
        }}
        finally
        {{
            if (result.attachedForeground)
                AttachThreadInput(currentThread, foregroundThread, false);
            if (result.attachedTarget)
                AttachThreadInput(currentThread, targetThread, false);
        }}

        return result;
    }}

    static bool IsTarget32Bit(IntPtr process)
    {{
        if (IntPtr.Size == 4)
            return true;
        bool wow64;
        if (IsWow64Process(process, out wow64))
            return wow64;
        return false;
    }}

    static string CheckboxState(int stateImageIndex)
    {{
        switch (stateImageIndex)
        {{
            case 0: return "none";
            case 1: return "unchecked";
            case 2: return "checked";
            case 3: return "mixed";
            default: return "stateImage" + stateImageIndex.ToString();
        }}
    }}

    static bool DesiredStateReached(string action, ItemState before, ItemState after)
    {{
        if (after == null)
            return false;
        if (action == "check")
            return after.checkboxState == "checked";
        if (action == "uncheck")
            return after.checkboxState == "unchecked";
        if (action == "toggle")
            return before != null && before.checkboxState != after.checkboxState;
        return true;
    }}

    static string WindowText(IntPtr hwnd)
    {{
        var sb = new StringBuilder(512);
        GetWindowTextW(hwnd, sb, sb.Capacity);
        return sb.ToString();
    }}

    static string ClassName(IntPtr hwnd)
    {{
        var sb = new StringBuilder(256);
        GetClassNameW(hwnd, sb, sb.Capacity);
        return sb.ToString();
    }}

    static string PtrString(IntPtr ptr)
    {{
        return "0x" + ptr.ToInt64().ToString("X");
    }}

    static IntPtr Add(IntPtr ptr, int offset)
    {{
        return new IntPtr(ptr.ToInt64() + offset);
    }}

    static IntPtr MakeLParam(int x, int y)
    {{
        return new IntPtr((y << 16) | (x & 0xFFFF));
    }}

    static void PutUInt32(byte[] buffer, int offset, uint value)
    {{
        Array.Copy(BitConverter.GetBytes(value), 0, buffer, offset, 4);
    }}

    static void PutInt32(byte[] buffer, int offset, int value)
    {{
        Array.Copy(BitConverter.GetBytes(value), 0, buffer, offset, 4);
    }}

    static void PutPtr(byte[] buffer, int offset, IntPtr value, bool target32Bit)
    {{
        long raw = value.ToInt64();
        if (target32Bit)
            Array.Copy(BitConverter.GetBytes((uint)raw), 0, buffer, offset, 4);
        else
            Array.Copy(BitConverter.GetBytes(raw), 0, buffer, offset, 8);
    }}

    static IntPtr GetPtr(byte[] buffer, int offset, bool target32Bit)
    {{
        if (target32Bit)
            return new IntPtr((long)BitConverter.ToUInt32(buffer, offset));
        return new IntPtr(BitConverter.ToInt64(buffer, offset));
    }}

    static void Write(IntPtr process, IntPtr address, byte[] data)
    {{
        IntPtr written;
        if (!WriteProcessMemory(process, address, data, data.Length, out written))
            throw new InvalidOperationException("WriteProcessMemory failed: " + Marshal.GetLastWin32Error());
    }}

    static byte[] Read(IntPtr process, IntPtr address, int length)
    {{
        byte[] data = new byte[length];
        IntPtr read;
        if (!ReadProcessMemory(process, address, data, data.Length, out read))
            throw new InvalidOperationException("ReadProcessMemory failed: " + Marshal.GetLastWin32Error());
        return data;
    }}

    static string DecodeNullTerminatedUnicode(byte[] data)
    {{
        int length = 0;
        while (length + 1 < data.Length)
        {{
            if (data[length] == 0 && data[length + 1] == 0)
                break;
            length += 2;
        }}
        return Encoding.Unicode.GetString(data, 0, length);
    }}
}}
'@

$result = [Win32TreeViewAction]::Act(
    [string]$Criteria.action,
    [string]$Criteria.windowTitleContains,
    [string]$Criteria.text,
    [string]$Criteria.textContains,
    [string]$Criteria.handle,
    [int]$Criteria.index,
    [int]$Criteria.maxNodes,
    [string]$Criteria.clickMethod
)
$result | ConvertTo-Json -Depth 80
}} catch {{
    [ordered]@{{
        ok = $false
        error = $_.Exception.Message
        detail = $_.ScriptStackTrace
    }} | ConvertTo-Json -Depth 20
}}
"""
