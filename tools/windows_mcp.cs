using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Drawing.Imaging;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Windows.Automation;
using System.Windows.Forms;
using System.Web.Script.Serialization;

internal static class NativeMethods
{
    [DllImport("user32.dll")] internal static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")] internal static extern bool ShowWindowAsync(IntPtr hWnd, int nCmdShow);
    [DllImport("user32.dll")] internal static extern bool IsIconic(IntPtr hWnd);
    [DllImport("user32.dll")] internal static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] internal static extern bool SetCursorPos(int x, int y);
    [DllImport("user32.dll")] internal static extern void mouse_event(uint flags, uint dx, uint dy, uint data, UIntPtr extraInfo);
    internal const int SW_RESTORE = 9;
    internal const uint MOUSEEVENTF_LEFTDOWN = 0x0002;
    internal const uint MOUSEEVENTF_LEFTUP = 0x0004;
    internal const uint MOUSEEVENTF_RIGHTDOWN = 0x0008;
    internal const uint MOUSEEVENTF_RIGHTUP = 0x0010;
    internal const uint MOUSEEVENTF_MIDDLEDOWN = 0x0020;
    internal const uint MOUSEEVENTF_MIDDLEUP = 0x0040;
    internal const uint MOUSEEVENTF_WHEEL = 0x0800;
}

internal sealed class Node
{
    internal AutomationElement Element;
    internal int Depth;
    internal Node(AutomationElement element, int depth) { Element = element; Depth = depth; }
}

internal static class Program
{
    private static readonly JavaScriptSerializer Json = new JavaScriptSerializer { MaxJsonLength = 16 * 1024 * 1024, RecursionLimit = 100 };
    private static readonly TreeWalker Walker = TreeWalker.ControlViewWalker;

    [STAThread]
    private static void Main()
    {
        Console.InputEncoding = new UTF8Encoding(false);
        Console.OutputEncoding = new UTF8Encoding(false);
        string line;
        while ((line = Console.ReadLine()) != null)
        {
            if (String.IsNullOrWhiteSpace(line)) continue;
            IDictionary<string, object> request = null;
            object id = null;
            try
            {
                request = Deserialize(line) as IDictionary<string, object>;
                if (request == null) throw new InvalidOperationException("Invalid JSON-RPC request");
                request.TryGetValue("id", out id);
                string method = GetString(request, "method", "");
                if (method == "initialize")
                {
                    SendResult(id, Obj(
                        "protocolVersion", "2025-03-26",
                        "capabilities", Obj("tools", Obj()),
                        "serverInfo", Obj("name", "windows-automation-mcp", "version", "2.0.0")));
                }
                else if (method == "tools/list")
                {
                    SendResult(id, Obj("tools", GetTools()));
                }
                else if (method == "tools/call")
                {
                    IDictionary<string, object> parameters = GetDict(request, "params");
                    string name = GetString(parameters, "name", "");
                    IDictionary<string, object> arguments = GetDict(parameters, "arguments");
                    SendResult(id, CallTool(name, arguments));
                }
                else if (method == "ping")
                {
                    SendResult(id, Obj());
                }
                else if (id != null)
                {
                    SendError(id, "Method not found: " + method, -32601);
                }
            }
            catch (Exception ex)
            {
                if (id != null) SendError(id, InnermostMessage(ex), -32000);
            }
        }
    }

    private static string InnermostMessage(Exception ex)
    {
        while (ex.InnerException != null) ex = ex.InnerException;
        return ex.Message;
    }

    private static string Serialize(object value)
    {
        return Json.Serialize(value);
    }

    private static object Deserialize(string text)
    {
        return Json.DeserializeObject(text);
    }

    private static Dictionary<string, object> Obj(params object[] values)
    {
        Dictionary<string, object> result = new Dictionary<string, object>();
        for (int i = 0; i + 1 < values.Length; i += 2) result[(string)values[i]] = values[i + 1];
        return result;
    }

    private static IDictionary<string, object> GetDict(IDictionary<string, object> source, string key)
    {
        if (source == null) return new Dictionary<string, object>();
        object value;
        if (!source.TryGetValue(key, out value) || value == null) return new Dictionary<string, object>();
        IDictionary<string, object> dictionary = value as IDictionary<string, object>;
        return dictionary ?? new Dictionary<string, object>();
    }

    private static string GetString(IDictionary<string, object> source, string key, string fallback)
    {
        object value;
        return source != null && source.TryGetValue(key, out value) && value != null ? Convert.ToString(value) : fallback;
    }

    private static int GetInt(IDictionary<string, object> source, string key, int fallback)
    {
        object value;
        if (source == null || !source.TryGetValue(key, out value) || value == null) return fallback;
        return Convert.ToInt32(value);
    }

    private static double GetDouble(IDictionary<string, object> source, string key, double fallback)
    {
        object value;
        if (source == null || !source.TryGetValue(key, out value) || value == null) return fallback;
        return Convert.ToDouble(value);
    }

    private static bool GetBool(IDictionary<string, object> source, string key, bool fallback)
    {
        object value;
        if (source == null || !source.TryGetValue(key, out value) || value == null) return fallback;
        return Convert.ToBoolean(value);
    }

    private static bool Has(IDictionary<string, object> source, string key)
    {
        object value;
        return source != null && source.TryGetValue(key, out value) && value != null && !String.IsNullOrWhiteSpace(Convert.ToString(value));
    }

    private static void SendResult(object id, object result)
    {
        Console.WriteLine(Serialize(Obj("jsonrpc", "2.0", "id", id, "result", result)));
        Console.Out.Flush();
    }

    private static void SendError(object id, string message, int code)
    {
        Console.WriteLine(Serialize(Obj("jsonrpc", "2.0", "id", id, "error", Obj("code", code, "message", message))));
        Console.Out.Flush();
    }

    private static object TextResult(object value)
    {
        return Obj("content", new object[] { Obj("type", "text", "text", Serialize(value)) }, "structuredContent", value);
    }

    private static object Tool(string name, string description, object schema)
    {
        return Obj("name", name, "description", description, "inputSchema", schema);
    }

    private static object Schema(Dictionary<string, object> properties, params string[] required)
    {
        Dictionary<string, object> result = Obj("type", "object", "properties", properties, "additionalProperties", false);
        if (required != null && required.Length > 0) result["required"] = required;
        return result;
    }

    private static Dictionary<string, object> SelectorProperties()
    {
        return Obj(
            "processId", Obj("type", "integer", "description", "Target process ID"),
            "processName", Obj("type", "string", "description", "Executable name, with or without .exe; resolves the unique visible process"),
            "windowName", Obj("type", "string", "description", "Exact top-level window title"),
            "title", Obj("type", "string", "description", "Top-level window title substring"),
            "name", Obj("type", "string", "description", "Exact accessible name"),
            "controlType", Obj("type", "string", "description", "Window, Button, Edit, DataItem, RadioButton, CheckBox, etc."),
            "automationId", Obj("type", "string", "description", "Exact stable UI Automation ID"),
            "className", Obj("type", "string", "description", "Exact native/UIA class name"),
            "occurrence", Obj("type", "integer", "minimum", 0, "default", 0),
            "maxDepth", Obj("type", "integer", "minimum", 0, "maximum", 64, "default", 12),
            "scanLimit", Obj("type", "integer", "minimum", 1, "maximum", 20000, "default", 5000),
            "timeoutSeconds", Obj("type", "number", "minimum", 0.1, "maximum", 120, "default", 15)
        );
    }

    private static Dictionary<string, object> Merge(Dictionary<string, object> left, params object[] values)
    {
        Dictionary<string, object> result = new Dictionary<string, object>(left);
        for (int i = 0; i + 1 < values.Length; i += 2) result[(string)values[i]] = values[i + 1];
        return result;
    }

    private static object[] GetTools()
    {
        Dictionary<string, object> selector = SelectorProperties();
        return new object[]
        {
            Tool("list_windows", "List visible top-level windows; filter by process ID, executable name, or title.", Schema(Obj(
                "title", Obj("type", "string"), "windowName", Obj("type", "string"), "processId", Obj("type", "integer"), "processName", Obj("type", "string")))),
            Tool("list_controls", "List accessible controls with bounded traversal; supports complex browser and Qt windows.", Schema(Merge(selector,
                "limit", Obj("type", "integer", "minimum", 1, "maximum", 1000, "default", 250)))),
            Tool("get_control_details", "Read value and selection/toggle state; password values are redacted.", Schema(selector)),
            Tool("invoke_control", "Invoke a button, menu item, or other accessible control without coordinates.", Schema(selector)),
            Tool("set_control_value", "Reliably set Unicode text with ValuePattern or clipboard fallback; optionally submit with Enter.", Schema(Merge(selector,
                "value", Obj("type", "string"), "submit", Obj("type", "boolean", "default", false),
                "fallbackToClipboard", Obj("type", "boolean", "default", true), "clear", Obj("type", "boolean", "default", true),
                "preserveClipboard", Obj("type", "boolean", "default", true)), "value")),
            Tool("select_control", "Select an item or set a checkbox toggle state.", Schema(Merge(selector, "state", Obj("type", "boolean", "default", true)))),
            Tool("click_control", "Click the safe center of a semantically resolved control.", Schema(Merge(selector,
                "button", Obj("type", "string", "enum", new string[] { "left", "right", "middle" }, "default", "left"),
                "count", Obj("type", "integer", "minimum", 1, "maximum", 3, "default", 1)))),
            Tool("send_keys", "Activate a target window or control and send Windows SendKeys syntax.", Schema(Merge(selector, "keys", Obj("type", "string")), "keys")),
            Tool("wait_for_control", "Wait for a matching control and optionally for enabled state.", Schema(Merge(selector, "enabled", Obj("type", "boolean")))),
            Tool("capture_screenshot", "Capture a compact target-window or desktop screenshot.", Schema(Obj(
                "processId", Obj("type", "integer"), "processName", Obj("type", "string"), "windowName", Obj("type", "string"), "title", Obj("type", "string"),
                "desktop", Obj("type", "boolean", "default", false), "maxWidth", Obj("type", "integer", "minimum", 320, "maximum", 3840, "default", 1280),
                "format", Obj("type", "string", "enum", new string[] { "jpeg", "png" }, "default", "jpeg"),
                "saveTo", Obj("type", "string", "description", "Optional absolute path; existing files are not overwritten"),
                "includeImage", Obj("type", "boolean", "default", true)))),
            Tool("activate_window", "Restore and foreground one target window by process, executable, or title.", Schema(selector)),
            Tool("focus_control", "Activate the target window and focus a semantically resolved control.", Schema(selector)),
            Tool("type_text", "Reliably enter Unicode text using ValuePattern or clipboard fallback; optionally submit with Enter.", Schema(Merge(selector,
                "text", Obj("type", "string"), "clear", Obj("type", "boolean", "default", true),
                "submit", Obj("type", "boolean", "default", false), "preserveClipboard", Obj("type", "boolean", "default", true)), "text")),
            Tool("click_point", "Activate an optional target and click an explicit screen point for canvas or inaccessible UI.", Schema(Obj(
                "processId", Obj("type", "integer"), "processName", Obj("type", "string"), "windowName", Obj("type", "string"), "title", Obj("type", "string"),
                "x", Obj("type", "integer"), "y", Obj("type", "integer"),
                "button", Obj("type", "string", "enum", new string[] { "left", "right", "middle" }, "default", "left"),
                "count", Obj("type", "integer", "minimum", 1, "maximum", 3, "default", 1)), "x", "y")),
            Tool("scroll", "Activate an optional target and send a mouse-wheel delta at the current or specified point.", Schema(Obj(
                "processId", Obj("type", "integer"), "processName", Obj("type", "string"), "windowName", Obj("type", "string"), "title", Obj("type", "string"),
                "x", Obj("type", "integer"), "y", Obj("type", "integer"), "delta", Obj("type", "integer", "minimum", -12000, "maximum", 12000)), "delta")),
            Tool("get_foreground_window", "Return the current foreground window and owning process.", Schema(Obj()))
        };
    }

    private static string TypeName(AutomationElement element)
    {
        try { return element.Current.ControlType.ProgrammaticName.Replace("ControlType.", ""); }
        catch { return "Unknown"; }
    }

    private static bool Finite(double value)
    {
        return !Double.IsNaN(value) && !Double.IsInfinity(value);
    }

    private static bool UsableRect(System.Windows.Rect rect)
    {
        return Finite(rect.Left) && Finite(rect.Top) && Finite(rect.Width) && Finite(rect.Height) &&
               rect.Width > 0 && rect.Height > 0 && rect.Left > Int32.MinValue && rect.Left < Int32.MaxValue &&
               rect.Top > Int32.MinValue && rect.Top < Int32.MaxValue && rect.Right > Int32.MinValue && rect.Right < Int32.MaxValue &&
               rect.Bottom > Int32.MinValue && rect.Bottom < Int32.MaxValue;
    }

    private static object RectData(System.Windows.Rect rect)
    {
        if (!UsableRect(rect)) return null;
        return Obj("left", Math.Round(rect.Left), "top", Math.Round(rect.Top), "width", Math.Round(rect.Width), "height", Math.Round(rect.Height));
    }

    private static object ElementData(AutomationElement element)
    {
        try
        {
            List<string> patterns = new List<string>();
            foreach (AutomationPattern pattern in element.GetSupportedPatterns())
                patterns.Add(pattern.ProgrammaticName.Replace("PatternIdentifiers.Pattern", ""));
            AutomationElement.AutomationElementInformation current = element.Current;
            return Obj(
                "name", current.Name, "controlType", TypeName(element), "automationId", current.AutomationId,
                "className", current.ClassName, "processId", current.ProcessId, "enabled", current.IsEnabled,
                "offscreen", current.IsOffscreen, "bounds", RectData(current.BoundingRectangle), "patterns", patterns.ToArray());
        }
        catch (ElementNotAvailableException)
        {
            return Obj("unavailable", true);
        }
    }

    private static bool Matches(AutomationElement element, IDictionary<string, object> args)
    {
        try
        {
            AutomationElement.AutomationElementInformation current = element.Current;
            if (Has(args, "name") && current.Name != GetString(args, "name", "")) return false;
            if (Has(args, "controlType") && TypeName(element) != GetString(args, "controlType", "")) return false;
            if (Has(args, "className") && current.ClassName != GetString(args, "className", "")) return false;
            if (Has(args, "automationId") && current.AutomationId != GetString(args, "automationId", "")) return false;
            return true;
        }
        catch { return false; }
    }

    private static string NormalizeProcessName(string name)
    {
        name = (name ?? "").Trim();
        if (name.EndsWith(".exe", StringComparison.OrdinalIgnoreCase)) name = name.Substring(0, name.Length - 4);
        return name;
    }

    private static int ResolveProcessId(IDictionary<string, object> args, bool required)
    {
        int explicitId = GetInt(args, "processId", 0);
        if (explicitId > 0)
        {
            Process.GetProcessById(explicitId);
            return explicitId;
        }
        string processName = NormalizeProcessName(GetString(args, "processName", ""));
        if (processName.Length == 0)
        {
            if (required) throw new InvalidOperationException("Specify processId, processName, windowName, or title");
            return 0;
        }
        List<Process> candidates = new List<Process>();
        string exactWindow = GetString(args, "windowName", "");
        string title = GetString(args, "title", "");
        foreach (Process process in Process.GetProcessesByName(processName))
        {
            try
            {
                if (process.MainWindowHandle == IntPtr.Zero) continue;
                if (exactWindow.Length > 0 && process.MainWindowTitle != exactWindow) continue;
                if (title.Length > 0 && process.MainWindowTitle.IndexOf(title, StringComparison.OrdinalIgnoreCase) < 0) continue;
                candidates.Add(process);
            }
            catch { }
        }
        if (candidates.Count == 1) return candidates[0].Id;
        if (candidates.Count == 0) throw new InvalidOperationException("No visible process found for processName=" + processName);
        List<string> labels = new List<string>();
        foreach (Process process in candidates) labels.Add(process.Id + ":" + process.MainWindowTitle);
        throw new InvalidOperationException("Expected one visible process, found " + candidates.Count + ": " + String.Join(", ", labels.ToArray()));
    }

    private static List<AutomationElement> TargetWindows(IDictionary<string, object> args, bool allowAll)
    {
        int processId = ResolveProcessId(args, false);
        string processName = NormalizeProcessName(GetString(args, "processName", ""));
        string exactWindow = GetString(args, "windowName", "");
        string title = GetString(args, "title", "");
        if (!allowAll && processId <= 0 && processName.Length == 0 && exactWindow.Length == 0 && title.Length == 0)
            throw new InvalidOperationException("Specify processId, processName, windowName, or title");
        AutomationElementCollection collection = AutomationElement.RootElement.FindAll(TreeScope.Children, Condition.TrueCondition);
        List<AutomationElement> result = new List<AutomationElement>();
        for (int i = 0; i < collection.Count; i++)
        {
            AutomationElement element = collection[i];
            try
            {
                AutomationElement.AutomationElementInformation current = element.Current;
                if (processId > 0 && current.ProcessId != processId) continue;
                if (processName.Length > 0)
                {
                    string actual = NormalizeProcessName(Process.GetProcessById(current.ProcessId).ProcessName);
                    if (!actual.Equals(processName, StringComparison.OrdinalIgnoreCase)) continue;
                }
                if (exactWindow.Length > 0 && current.Name != exactWindow) continue;
                if (title.Length > 0 && current.Name.IndexOf(title, StringComparison.OrdinalIgnoreCase) < 0) continue;
                result.Add(element);
            }
            catch { }
        }
        return result;
    }

    private static IEnumerable<AutomationElement> ProcessElements(IDictionary<string, object> args)
    {
        List<AutomationElement> windows = TargetWindows(args, false);
        int maxDepth = Math.Max(0, Math.Min(64, GetInt(args, "maxDepth", 12)));
        int scanLimit = Math.Max(1, Math.Min(20000, GetInt(args, "scanLimit", 5000)));
        double timeout = Math.Max(0.1, Math.Min(120, GetDouble(args, "timeoutSeconds", 15)));
        Stopwatch stopwatch = Stopwatch.StartNew();
        int scanned = 0;
        Queue<Node> queue = new Queue<Node>();
        foreach (AutomationElement window in windows) queue.Enqueue(new Node(window, 0));
        while (queue.Count > 0 && scanned < scanLimit)
        {
            if (stopwatch.Elapsed.TotalSeconds > timeout) throw new TimeoutException("UI traversal timed out after " + timeout + " seconds (scanned=" + scanned + ")");
            Node node = queue.Dequeue();
            scanned++;
            yield return node.Element;
            if (node.Depth >= maxDepth) continue;
            AutomationElement child = null;
            try { child = Walker.GetFirstChild(node.Element); } catch { child = null; }
            while (child != null && scanned + queue.Count < scanLimit)
            {
                queue.Enqueue(new Node(child, node.Depth + 1));
                AutomationElement next = null;
                try { next = Walker.GetNextSibling(child); } catch { next = null; }
                child = next;
            }
        }
    }

    private static AutomationElement FindControl(IDictionary<string, object> args)
    {
        int occurrence = Math.Max(0, GetInt(args, "occurrence", 0));
        int found = 0;
        foreach (AutomationElement element in ProcessElements(args))
        {
            if (!Matches(element, args)) continue;
            if (found == occurrence) return element;
            found++;
        }
        throw new InvalidOperationException("Control not found (matches=" + found + ", occurrence=" + occurrence + ")");
    }

    private static object ListWindows(IDictionary<string, object> args)
    {
        List<object> result = new List<object>();
        foreach (AutomationElement element in TargetWindows(args, true))
        {
            try { if (!element.Current.IsOffscreen && !String.IsNullOrWhiteSpace(element.Current.Name)) result.Add(ElementData(element)); }
            catch { }
        }
        return Obj("windows", result.ToArray());
    }

    private static object ListControls(IDictionary<string, object> args)
    {
        int limit = Math.Max(1, Math.Min(1000, GetInt(args, "limit", 250)));
        List<object> result = new List<object>();
        foreach (AutomationElement element in ProcessElements(args))
        {
            if (Matches(element, args)) result.Add(ElementData(element));
            if (result.Count >= limit) break;
        }
        return Obj("controls", result.ToArray());
    }

    private static AutomationElement ActivateWindow(IDictionary<string, object> args)
    {
        List<AutomationElement> windows = TargetWindows(args, false);
        if (windows.Count == 0) throw new InvalidOperationException("Target window not found");
        AutomationElement window = windows[0];
        int handle = window.Current.NativeWindowHandle;
        if (handle == 0) throw new InvalidOperationException("Target window has no native handle");
        IntPtr hwnd = new IntPtr(handle);
        if (NativeMethods.IsIconic(hwnd)) NativeMethods.ShowWindowAsync(hwnd, NativeMethods.SW_RESTORE);
        NativeMethods.SetForegroundWindow(hwnd);
        try { window.SetFocus(); } catch { }
        Thread.Sleep(80);
        return window;
    }

    private static object FocusControl(IDictionary<string, object> args)
    {
        AutomationElement element = FindControl(args);
        try { ActivateWindow(Obj("processId", element.Current.ProcessId)); } catch { }
        element.SetFocus();
        return ElementData(element);
    }

    private static object InvokeControl(IDictionary<string, object> args)
    {
        AutomationElement element = FindControl(args);
        object raw;
        if (!element.TryGetCurrentPattern(InvokePattern.Pattern, out raw)) throw new InvalidOperationException("Control does not support InvokePattern");
        ((InvokePattern)raw).Invoke();
        return ElementData(element);
    }

    private static object SetControlValue(IDictionary<string, object> args)
    {
        AutomationElement element = FindControl(args);
        try { ActivateWindow(Obj("processId", element.Current.ProcessId)); } catch { }
        try { element.SetFocus(); } catch { }
        string value = GetString(args, "value", "");
        string method = "ValuePattern";
        object raw;
        if (element.TryGetCurrentPattern(ValuePattern.Pattern, out raw) && !((ValuePattern)raw).Current.IsReadOnly)
        {
            ((ValuePattern)raw).SetValue(value);
        }
        else
        {
            if (!GetBool(args, "fallbackToClipboard", true)) throw new InvalidOperationException("Control has no writable ValuePattern");
            method = "clipboard";
            bool preserve = GetBool(args, "preserveClipboard", true);
            string oldText = null;
            bool hadText = false;
            if (preserve)
            {
                try { hadText = Clipboard.ContainsText(); if (hadText) oldText = Clipboard.GetText(); } catch { preserve = false; }
            }
            try
            {
                Clipboard.SetText(value);
                if (GetBool(args, "clear", true)) SendKeys.SendWait("^a");
                SendKeys.SendWait("^v");
            }
            finally
            {
                if (preserve)
                {
                    Thread.Sleep(80);
                    try { if (hadText) Clipboard.SetText(oldText ?? ""); else Clipboard.Clear(); } catch { }
                }
            }
        }
        try { element.SetFocus(); } catch { }
        bool submitted = GetBool(args, "submit", false);
        if (submitted) SendKeys.SendWait("{ENTER}");
        return Obj("entered", true, "submitted", submitted, "method", method, "control", ElementData(element));
    }

    private static object SelectControl(IDictionary<string, object> args)
    {
        AutomationElement element = FindControl(args);
        object raw;
        if (element.TryGetCurrentPattern(SelectionItemPattern.Pattern, out raw))
        {
            ((SelectionItemPattern)raw).Select();
        }
        else if (element.TryGetCurrentPattern(TogglePattern.Pattern, out raw))
        {
            TogglePattern toggle = (TogglePattern)raw;
            bool wanted = GetBool(args, "state", true);
            bool isOn = toggle.Current.ToggleState == ToggleState.On;
            if (isOn != wanted) toggle.Toggle();
        }
        else throw new InvalidOperationException("Control supports neither SelectionItemPattern nor TogglePattern");
        return ElementData(element);
    }

    private static Point SafeCenter(System.Windows.Rect rect)
    {
        if (!UsableRect(rect)) throw new InvalidOperationException("Control has no finite clickable bounds");
        double x = rect.Left + rect.Width / 2.0;
        double y = rect.Top + rect.Height / 2.0;
        if (!Finite(x) || !Finite(y) || x < Int32.MinValue || x > Int32.MaxValue || y < Int32.MinValue || y > Int32.MaxValue)
            throw new InvalidOperationException("Control center is outside the Windows coordinate range");
        return new Point((int)Math.Round(x), (int)Math.Round(y));
    }

    private static void ClickAt(int x, int y, string button, int count)
    {
        uint down = NativeMethods.MOUSEEVENTF_LEFTDOWN, up = NativeMethods.MOUSEEVENTF_LEFTUP;
        if (button == "right") { down = NativeMethods.MOUSEEVENTF_RIGHTDOWN; up = NativeMethods.MOUSEEVENTF_RIGHTUP; }
        else if (button == "middle") { down = NativeMethods.MOUSEEVENTF_MIDDLEDOWN; up = NativeMethods.MOUSEEVENTF_MIDDLEUP; }
        if (!NativeMethods.SetCursorPos(x, y)) throw new InvalidOperationException("SetCursorPos failed");
        for (int i = 0; i < count; i++)
        {
            NativeMethods.mouse_event(down, 0, 0, 0, UIntPtr.Zero);
            NativeMethods.mouse_event(up, 0, 0, 0, UIntPtr.Zero);
            if (i + 1 < count) Thread.Sleep(80);
        }
    }

    private static object ClickControl(IDictionary<string, object> args)
    {
        AutomationElement element = FindControl(args);
        Point point = SafeCenter(element.Current.BoundingRectangle);
        try { ActivateWindow(Obj("processId", element.Current.ProcessId)); } catch { }
        ClickAt(point.X, point.Y, GetString(args, "button", "left"), Math.Max(1, Math.Min(3, GetInt(args, "count", 1))));
        return ElementData(element);
    }

    private static object SendKeysToTarget(IDictionary<string, object> args)
    {
        AutomationElement target = null;
        bool hasControlSelector = Has(args, "name") || Has(args, "automationId") || Has(args, "controlType") || Has(args, "className");
        if (hasControlSelector)
        {
            target = FindControl(args);
            try { ActivateWindow(Obj("processId", target.Current.ProcessId)); } catch { }
            target.SetFocus();
        }
        else ActivateWindow(args);
        Thread.Sleep(80);
        SendKeys.SendWait(GetString(args, "keys", ""));
        return Obj("activated", true, "sent", GetString(args, "keys", ""), "target", target == null ? null : ElementData(target));
    }

    private static object TypeText(IDictionary<string, object> args)
    {
        AutomationElement element = FindControl(args);
        try { ActivateWindow(Obj("processId", element.Current.ProcessId)); } catch { }
        try { element.SetFocus(); } catch { }
        string text = GetString(args, "text", "");
        string method = "clipboard";
        object raw;
        if (element.TryGetCurrentPattern(ValuePattern.Pattern, out raw) && !((ValuePattern)raw).Current.IsReadOnly)
        {
            ((ValuePattern)raw).SetValue(text);
            method = "ValuePattern";
        }
        else
        {
            bool preserve = GetBool(args, "preserveClipboard", true);
            string oldText = null;
            bool hadText = false;
            if (preserve)
            {
                try { hadText = Clipboard.ContainsText(); if (hadText) oldText = Clipboard.GetText(); } catch { preserve = false; }
            }
            try
            {
                Clipboard.SetText(text);
                if (GetBool(args, "clear", true)) SendKeys.SendWait("^a");
                SendKeys.SendWait("^v");
            }
            finally
            {
                if (preserve)
                {
                    Thread.Sleep(80);
                    try { if (hadText) Clipboard.SetText(oldText ?? ""); else Clipboard.Clear(); } catch { }
                }
            }
        }
        try { element.SetFocus(); } catch { }
        if (GetBool(args, "submit", false)) SendKeys.SendWait("{ENTER}");
        return Obj("entered", true, "submitted", GetBool(args, "submit", false), "method", method, "control", ElementData(element));
    }

    private static object WaitForControl(IDictionary<string, object> args)
    {
        double timeout = Math.Max(0.1, Math.Min(120, GetDouble(args, "timeoutSeconds", 15)));
        Stopwatch stopwatch = Stopwatch.StartNew();
        Exception last = null;
        while (stopwatch.Elapsed.TotalSeconds < timeout)
        {
            try
            {
                AutomationElement element = FindControl(args);
                if (!args.ContainsKey("enabled") || element.Current.IsEnabled == GetBool(args, "enabled", true)) return ElementData(element);
            }
            catch (Exception ex) { last = ex; }
            Thread.Sleep(200);
        }
        throw new TimeoutException("Timed out after " + timeout + " seconds waiting for control" + (last == null ? "" : ": " + InnermostMessage(last)));
    }

    private static Rectangle WindowRectangle(IDictionary<string, object> args)
    {
        List<AutomationElement> windows = TargetWindows(args, false);
        if (windows.Count == 0) throw new InvalidOperationException("Target window not found");
        System.Windows.Rect rect = windows[0].Current.BoundingRectangle;
        if (!UsableRect(rect)) throw new InvalidOperationException("Screenshot bounds are empty or non-finite");
        return Rectangle.FromLTRB((int)Math.Round(rect.Left), (int)Math.Round(rect.Top), (int)Math.Round(rect.Right), (int)Math.Round(rect.Bottom));
    }

    private static object Screenshot(IDictionary<string, object> args)
    {
        Rectangle rect = GetBool(args, "desktop", false) ? SystemInformation.VirtualScreen : WindowRectangle(args);
        if (rect.Width <= 0 || rect.Height <= 0) throw new InvalidOperationException("Screenshot bounds are empty");
        int maxWidth = Math.Max(320, Math.Min(3840, GetInt(args, "maxWidth", 1280)));
        string format = GetString(args, "format", "jpeg").ToLowerInvariant();
        if (format != "jpeg" && format != "png") throw new InvalidOperationException("format must be jpeg or png");
        byte[] bytes;
        int width, height;
        using (Bitmap bitmap = new Bitmap(rect.Width, rect.Height))
        {
            using (Graphics graphics = Graphics.FromImage(bitmap)) graphics.CopyFromScreen(rect.Left, rect.Top, 0, 0, rect.Size);
            Image output = bitmap;
            Bitmap small = null;
            if (rect.Width > maxWidth)
            {
                int scaledHeight = Math.Max(1, (int)Math.Round((double)rect.Height * maxWidth / rect.Width));
                small = new Bitmap(maxWidth, scaledHeight);
                using (Graphics graphics = Graphics.FromImage(small))
                {
                    graphics.InterpolationMode = InterpolationMode.HighQualityBicubic;
                    graphics.DrawImage(bitmap, 0, 0, maxWidth, scaledHeight);
                }
                output = small;
            }
            width = output.Width; height = output.Height;
            using (MemoryStream stream = new MemoryStream())
            {
                output.Save(stream, format == "png" ? ImageFormat.Png : ImageFormat.Jpeg);
                bytes = stream.ToArray();
            }
            if (small != null) small.Dispose();
        }
        string saved = null;
        if (Has(args, "saveTo"))
        {
            saved = Path.GetFullPath(GetString(args, "saveTo", ""));
            if (!Path.IsPathRooted(saved)) throw new InvalidOperationException("saveTo must be absolute");
            if (File.Exists(saved)) throw new InvalidOperationException("saveTo already exists; choose a new path");
            File.WriteAllBytes(saved, bytes);
        }
        List<object> content = new List<object>();
        content.Add(Obj("type", "text", "text", "Captured " + width + "x" + height + " " + format + " (" + bytes.Length + " bytes)"));
        if (GetBool(args, "includeImage", true)) content.Add(Obj("type", "image", "data", Convert.ToBase64String(bytes), "mimeType", "image/" + format));
        return Obj("content", content.ToArray(), "structuredContent", Obj(
            "width", width, "height", height, "originalWidth", rect.Width, "originalHeight", rect.Height,
            "bytes", bytes.Length, "format", format, "path", saved));
    }

    private static object ControlDetails(IDictionary<string, object> args)
    {
        AutomationElement element = FindControl(args);
        Dictionary<string, object> data = (Dictionary<string, object>)ElementData(element);
        bool password = false;
        try { password = element.Current.IsPassword; } catch { }
        data["isPassword"] = password;
        object raw;
        if (!password && element.TryGetCurrentPattern(ValuePattern.Pattern, out raw))
        {
            data["value"] = ((ValuePattern)raw).Current.Value;
            data["readOnly"] = ((ValuePattern)raw).Current.IsReadOnly;
        }
        if (element.TryGetCurrentPattern(TogglePattern.Pattern, out raw)) data["toggleState"] = ((TogglePattern)raw).Current.ToggleState.ToString();
        if (element.TryGetCurrentPattern(SelectionItemPattern.Pattern, out raw)) data["selected"] = ((SelectionItemPattern)raw).Current.IsSelected;
        return data;
    }

    private static object ClickPoint(IDictionary<string, object> args)
    {
        if (Has(args, "processId") || Has(args, "processName") || Has(args, "windowName") || Has(args, "title")) ActivateWindow(args);
        int x = GetInt(args, "x", 0), y = GetInt(args, "y", 0);
        ClickAt(x, y, GetString(args, "button", "left"), Math.Max(1, Math.Min(3, GetInt(args, "count", 1))));
        return Obj("clicked", true, "x", x, "y", y, "button", GetString(args, "button", "left"), "count", GetInt(args, "count", 1));
    }

    private static object Scroll(IDictionary<string, object> args)
    {
        if (Has(args, "processId") || Has(args, "processName") || Has(args, "windowName") || Has(args, "title")) ActivateWindow(args);
        if (args.ContainsKey("x") && args.ContainsKey("y"))
        {
            if (!NativeMethods.SetCursorPos(GetInt(args, "x", 0), GetInt(args, "y", 0))) throw new InvalidOperationException("SetCursorPos failed");
        }
        int delta = Math.Max(-12000, Math.Min(12000, GetInt(args, "delta", 0)));
        NativeMethods.mouse_event(NativeMethods.MOUSEEVENTF_WHEEL, 0, 0, unchecked((uint)delta), UIntPtr.Zero);
        return Obj("scrolled", true, "delta", delta);
    }

    private static object ForegroundWindow()
    {
        IntPtr hwnd = NativeMethods.GetForegroundWindow();
        if (hwnd == IntPtr.Zero) return Obj("window", null);
        AutomationElement element = AutomationElement.FromHandle(hwnd);
        object data = ElementData(element);
        int pid = 0;
        string processName = null;
        try { pid = element.Current.ProcessId; processName = Process.GetProcessById(pid).ProcessName; } catch { }
        return Obj("window", data, "processId", pid, "processName", processName);
    }

    private static object CallTool(string name, IDictionary<string, object> args)
    {
        switch (name)
        {
            case "list_windows": return TextResult(ListWindows(args));
            case "list_controls": return TextResult(ListControls(args));
            case "get_control_details": return TextResult(ControlDetails(args));
            case "invoke_control": return TextResult(InvokeControl(args));
            case "set_control_value": return TextResult(SetControlValue(args));
            case "select_control": return TextResult(SelectControl(args));
            case "click_control": return TextResult(ClickControl(args));
            case "send_keys": return TextResult(SendKeysToTarget(args));
            case "wait_for_control": return TextResult(WaitForControl(args));
            case "capture_screenshot": return Screenshot(args);
            case "activate_window": return TextResult(ElementData(ActivateWindow(args)));
            case "focus_control": return TextResult(FocusControl(args));
            case "type_text": return TextResult(TypeText(args));
            case "click_point": return TextResult(ClickPoint(args));
            case "scroll": return TextResult(Scroll(args));
            case "get_foreground_window": return TextResult(ForegroundWindow());
            default: throw new InvalidOperationException("Unknown tool: " + name);
        }
    }
}