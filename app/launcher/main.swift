// Miloco 独立 App 启动器（macOS / Apple Silicon）
//
// 职责：作为 Miloco 后端的父进程，提供菜单栏/停靠栏入口与完整的生命周期管理——
// 启动、停止、重启、异常退出自动重启、健康巡检、单实例、端口回退、日志落盘、
// 开机自启开关。不依赖 supervisor / launchd / 任何外部进程管理器。
//
// 用法：
//   Miloco                正常启动（菜单栏常驻，无窗口）
//   Miloco --selftest     只跑非 UI 自检（路径/配置/端口探测）后退出，供构建冒烟测试
//
// 环境注入：MILOCO_HOME / MILOCO_EDITION=slim / MILOCO_SERVER__HOST|PORT。
// 刻意不设 MILOCO_SUPERVISED：后端 bootstrap 会把 stdout/stderr 重定向到
// $MILOCO_HOME/log/miloco-backend_<ts>.log（非 tty 时），这正是我们要的。

import AppKit
import Foundation
import WebKit

// MARK: - 常量

let kBundleID = "com.xiaomi.miloco"
/// App 的固定端口，刻意和 CLI / supervisor 版（1810）分开：两条链路可以同时跑，
/// 谁也不会接到谁的后端上。只有 1812 被**非 Miloco** 进程占用时才顺延到 1813…，
/// 并在日志与菜单里说明实际端口。
let kPreferredPort = 1812
let kPortRange = kPreferredPort...(kPreferredPort + 10)
/// 界面语言：默认英文，跟随系统首选语言；只支持 en / 中文两种。
///
/// 需要在打包后的 App 上验证另一种语言时，可以用环境变量强制：
///   MILOCO_APP_LANG=zh /Applications/Miloco.app/Contents/MacOS/Miloco
/// （`-AppleLanguages '(zh-Hans)'` 也有效，因为读的就是 Locale.preferredLanguages。）
enum AppLanguage {
    /// UserDefaults 里的显式选择："system" / "zh" / "en"（菜单「语言」写入）。
    static let overrideKey = "langOverride"

    static var override: String {
        get {
            let v = UserDefaults.standard.string(forKey: overrideKey) ?? "system"
            return (v == "zh" || v == "en") ? v : "system"
        }
        set { UserDefaults.standard.set(newValue, forKey: overrideKey) }
    }

    /// 排查/测试用的强制覆盖。
    static var envOverride: String? {
        guard let forced = ProcessInfo.processInfo.environment["MILOCO_APP_LANG"]?.lowercased()
        else { return nil }
        if forced.hasPrefix("zh") { return "zh" }
        if forced.hasPrefix("en") { return "en" }
        return nil
    }

    /// 解析顺序：环境变量 > 菜单里选的语言 > 系统首选语言 > 英文（默认英文）。
    static var code: String {
        if let env = envOverride { return env }
        if override != "system" { return override }
        for lang in Locale.preferredLanguages {
            let l = lang.lowercased()
            if l.hasPrefix("zh") { return "zh" }
            if l.hasPrefix("en") { return "en" }
        }
        return "en"
    }

    static var isChinese: Bool { code == "zh" }

    /// 菜单「语言」当前该打勾的项。
    static var menuSelection: String {
        if let env = envOverride { return env }
        return override
    }
}

/// 行内双语：`t("English", "中文")`。界面字符串不多，直接写在调用点比维护
/// 一堆 key + 资源文件更不容易漏译，也不用动打包流程。
func t(_ en: String, _ zh: String) -> String {
    AppLanguage.isChinese ? zh : en
}

/// 展示用的版本号（来自 Info.plist），只用于日志与内置窗口的 UA。
var kAppVersion: String {
    (Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String) ?? "0"
}
let kShowPageNotification = Notification.Name("com.xiaomi.miloco.showPage")
let kMaxConsecutiveRestarts = 5
let kHealthyResetInterval: TimeInterval = 120
let kHealthCheckInterval: TimeInterval = 15
let kHealthFailuresBeforeRestart = 3
let kStartupTimeout: TimeInterval = 90

// MARK: - 路径

enum AppPaths {
    static let resources = Bundle.main.resourceURL
        ?? URL(fileURLWithPath: FileManager.default.currentDirectoryPath)

    static var python: URL { resources.appendingPathComponent("py/bin/python3") }
    static var defaultsConfig: URL { resources.appendingPathComponent("defaults/config.json") }

    static var home: URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/Miloco", isDirectory: true)
    }

    static var configFile: URL { home.appendingPathComponent("config.json") }
    static var logDir: URL { home.appendingPathComponent("log", isDirectory: true) }
    static var launcherLog: URL { logDir.appendingPathComponent("launcher.log") }

    static var launchAgentPlist: URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/LaunchAgents/\(kBundleID).plist")
    }
}

// MARK: - 日志

enum Log {
    private static let formatter: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime]
        return f
    }()

    static func line(_ message: String) {
        let text = "[\(formatter.string(from: Date()))] \(message)\n"
        let data = Data(text.utf8)
        try? FileManager.default.createDirectory(
            at: AppPaths.logDir, withIntermediateDirectories: true)
        if let handle = try? FileHandle(forWritingTo: AppPaths.launcherLog) {
            handle.seekToEndOfFile()
            handle.write(data)
            try? handle.close()
        } else {
            try? data.write(to: AppPaths.launcherLog)
        }
        FileHandle.standardError.write(data)
    }
}

// MARK: - 小工具

func isPortFree(_ port: Int) -> Bool {
    let fd = socket(AF_INET, SOCK_STREAM, 0)
    guard fd >= 0 else { return false }
    defer { close(fd) }
    var reuse: Int32 = 1
    setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &reuse, socklen_t(MemoryLayout<Int32>.size))
    var addr = sockaddr_in()
    addr.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
    addr.sin_family = sa_family_t(AF_INET)
    addr.sin_port = in_port_t(UInt16(port).bigEndian)
    addr.sin_addr.s_addr = inet_addr("127.0.0.1")
    let result = withUnsafePointer(to: &addr) { pointer in
        pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) {
            bind(fd, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
        }
    }
    return result == 0
}

/// 同步探活；返回 HTTP 状态码，超时/拒绝连接返回 nil。
func httpStatus(port: Int, path: String, timeout: TimeInterval = 1.5, bearer: String? = nil) -> Int? {
    guard let url = URL(string: "http://127.0.0.1:\(port)\(path)") else { return nil }
    var request = URLRequest(url: url)
    request.timeoutInterval = timeout
    request.cachePolicy = .reloadIgnoringLocalCacheData
    if let bearer { request.setValue("Bearer \(bearer)", forHTTPHeaderField: "Authorization") }
    let semaphore = DispatchSemaphore(value: 0)
    var status: Int?
    let task = URLSession.shared.dataTask(with: request) { _, response, _ in
        status = (response as? HTTPURLResponse)?.statusCode
        semaphore.signal()
    }
    task.resume()
    _ = semaphore.wait(timeout: .now() + timeout + 1.0)
    return status
}

func isMilocoHealthy(port: Int) -> Bool {
    guard let status = httpStatus(port: port, path: "/health", timeout: 1.0) else { return false }
    return status == 200
}

/// 本 App 数据目录里的 server.token（首启还没 bootstrap 时为 nil）。
func ourServerToken() -> String? {
    guard let data = try? Data(contentsOf: AppPaths.configFile),
        let root = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
        let server = root["server"] as? [String: Any],
        let token = server["token"] as? String, !token.isEmpty
    else { return nil }
    return token
}

/// 端口上那个健康的 Miloco 是不是**本 App 数据目录**的后端？
///
/// 判据是鉴权：`/api/admin/edition` 需要 server.token，而 token 只存在于本数据目录的
/// config.json 里 —— 401 就说明那是另一个安装（例如 CLI 版）的后端。这个区分对「独立
/// App」很重要：复用别人的后端会让住户在本 App 里看到 CLI 版的账号与数据，正是我们要
/// 避免的那种"数据说不清归属"的状态。
func isOurBackend(port: Int) -> Bool {
    guard let token = ourServerToken() else { return false }
    return httpStatus(port: port, path: "/api/admin/edition", timeout: 1.5, bearer: token) == 200
}

// MARK: - 后端进程

final class BackendProcess {
    private(set) var process: Process?
    private(set) var port: Int = 0
    /// 进程非我们主动停止而退出时回调（用于自动重启）。
    var onUnexpectedExit: ((Int32) -> Void)?

    private var stopping = false
    private var pipe: Pipe?

    var isRunning: Bool { process?.isRunning ?? false }

    /// 返回可用的绑定端口：优先 1812（App 的固定端口），被占则依次往后试。
    static func pickPort() -> Int? {
        for port in kPortRange where isPortFree(port) { return port }
        return nil
    }

    /// 已在运行的 Miloco（例如上一次启动的实例仍活着）→ 直接接管为“运行中”而不重复启动。
    static func findRunningPort() -> Int? {
        for port in kPortRange where isMilocoHealthy(port: port) { return port }
        return nil
    }

    func start(port: Int) throws {
        guard !isRunning else { return }
        let task = Process()
        task.executableURL = AppPaths.python
        task.arguments = ["-m", "miloco.main"]
        task.currentDirectoryURL = AppPaths.home

        var env = ProcessInfo.processInfo.environment
        env["MILOCO_HOME"] = AppPaths.home.path
        env["MILOCO_EDITION"] = "slim"
        env["MILOCO_SERVER__HOST"] = "127.0.0.1"
        env["MILOCO_SERVER__PORT"] = String(port)
        // server.url 一并覆盖：后端启动时会校验 url 与 host/port 是否一致，不一致就
        // 打一条“CLI 使用 server.url 访问后端”的告警；端口回退到 1811+ 时必现。
        env["MILOCO_SERVER__URL"] = "http://127.0.0.1:\(port)"
        // 中文日志 + 不在 .app 内写 __pycache__（保持签名不变）。
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        // 后端靠 MILOCO_SUPERVISED 判断是否抑制 stdio→日志文件重定向；App 里要它重定向。
        env.removeValue(forKey: "MILOCO_SUPERVISED")
        task.environment = env

        let output = Pipe()
        task.standardOutput = output
        task.standardError = output
        task.standardInput = FileHandle.nullDevice
        pipe = output
        output.fileHandleForReading.readabilityHandler = { handle in
            let data = handle.availableData
            guard !data.isEmpty else { return }
            // 后端 bootstrap() 会把自身 stdio 重定向到自己的日志文件，这里通常收不到东西；
            // 但启动最早期（bootstrap 之前）的原生库输出会走这里，落 launcher.log 便于排查。
            if let text = String(data: data, encoding: .utf8) {
                FileHandle.standardError.write(Data(text.utf8))
            }
        }

        task.terminationHandler = { [weak self] proc in
            guard let self else { return }
            self.pipe?.fileHandleForReading.readabilityHandler = nil
            let code = proc.terminationStatus
            let userInitiated = self.stopping
            Log.line("后端进程退出：exit=\(code) 主动停止=\(userInitiated)")
            self.process = nil
            if !userInitiated {
                self.onUnexpectedExit?(code)
            }
        }

        try task.run()
        process = task
        self.port = port
        stopping = false
        Log.line("后端进程已启动：pid=\(task.processIdentifier) port=\(port)")
    }

    /// 优雅停止：SIGTERM → 等待 → SIGKILL。
    func stop(gracefulTimeout: TimeInterval = 25) {
        guard let task = process, task.isRunning else {
            process = nil
            return
        }
        stopping = true
        let pid = task.processIdentifier
        Log.line("停止后端进程 pid=\(pid)（SIGTERM）")
        task.terminate()
        let deadline = Date().addingTimeInterval(gracefulTimeout)
        while task.isRunning && Date() < deadline {
            usleep(200_000)
        }
        if task.isRunning {
            Log.line("优雅停止超时，SIGKILL pid=\(pid)")
            kill(pid, SIGKILL)
            usleep(200_000)
        }
        pipe?.fileHandleForReading.readabilityHandler = nil
        process = nil
    }
}

// MARK: - 内置管理页面窗口（WKWebView）

/// App 自带的管理页面窗口，行为接近 Electron / Tauri 的主窗口：不用跳浏览器。
/// 关窗只是隐藏（服务继续在菜单栏跑），点停靠栏图标或菜单「打开管理页面」可再打开。
final class AdminWindow: NSObject, WKNavigationDelegate, WKUIDelegate, NSWindowDelegate,
    WKScriptMessageHandler
{
    private var window: NSWindow?
    private var webView: WKWebView?
    private var loadedURL: URL?
    /// 页面里切了语言 → 通知原生（让菜单也跟着切）。
    var onLanguageReported: ((String) -> Void)?

    var isVisible: Bool { window?.isVisible ?? false }

    /// 强制重新加载（切语言时用；同一个 URL 也要重载，因为语言可能在 URL 之外变化）。
    func reload(url: URL) {
        ensureWindow()
        loadedURL = url
        Log.line("内置窗口重新加载 \(url.absoluteString)")
        webView?.load(URLRequest(url: url))
    }

    // MARK: 页面 → 原生：语言

    func userContentController(
        _ userContentController: WKUserContentController, didReceive message: WKScriptMessage
    ) {
        guard message.name == "milocoLang", let lang = message.body as? String else { return }
        onLanguageReported?(lang == "en" ? "en" : "zh")
    }

    /// 打开（或复用）窗口并加载管理页。
    func show(url: URL) {
        ensureWindow()
        if loadedURL?.absoluteString != url.absoluteString {
            loadedURL = url
            Log.line("内置窗口加载 \(url.absoluteString)")
            webView?.load(URLRequest(url: url))
        }
        focus()
    }

    /// 后端还没就绪时先显示占位，避免给住户一个「无法连接」的白屏。
    func showPlaceholder(_ text: String) {
        ensureWindow()
        loadedURL = nil
        webView?.loadHTMLString(
            """
            <html><head><meta charset="utf-8"><style>
            body{font:14px -apple-system,BlinkMacSystemFont,sans-serif;color:#666;
                 display:flex;align-items:center;justify-content:center;height:100vh;margin:0}
            </style></head><body><div>\(text)</div></body></html>
            """, baseURL: nil)
        focus()
    }

    /// 把窗口提到最前并抢焦点：双击 App 图标后应该直接看到窗口，而不是「打开了但
    /// 藏在别的窗口后面」。orderFrontRegardless 会让它越过其它 App 的窗口显示。
    private func focus() {
        NSApp.activate(ignoringOtherApps: true)
        window?.makeKeyAndOrderFront(nil)
        window?.orderFrontRegardless()
    }

    private func ensureWindow() {
        guard window == nil else { return }
        let config = WKWebViewConfiguration()
        // 摄像头实时画面要能自动起播，否则会被 WebKit 的自动播放策略拦下。
        config.mediaTypesRequiringUserActionForPlayback = []
        // 页面里切语言时回报原生（web/src/i18n 里调 window.webkit.messageHandlers.milocoLang）。
        config.userContentController.add(self, name: "milocoLang")
        let web = WKWebView(frame: NSRect(x: 0, y: 0, width: 1280, height: 860), configuration: config)
        web.navigationDelegate = self
        web.uiDelegate = self
        // UA 里带 App 名：后端访问日志里能区分「内置窗口」与「外部浏览器」，排查时很有用。
        web.customUserAgent = "Miloco-app/\(kAppVersion) (WKWebView; macOS)"
        if #available(macOS 13.3, *) { web.isInspectable = true }
        webView = web

        let win = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 1280, height: 860),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered, defer: false)
        win.title = t("Miloco", "Miloco")
        win.contentView = web
        win.minSize = NSSize(width: 900, height: 600)
        win.isReleasedWhenClosed = false
        win.delegate = self
        win.center()
        window = win
    }

    // MARK: 导航回调

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        guard let url = webView.url, url.scheme == "http" || url.scheme == "https" else {
            return  // 占位页（about:blank / loadHTMLString）不算「页面就绪」，不探测
        }
        // 打一条「真的渲染出来了」的日志：标题 + 正文长度 + 页面语言。晚 1.5s 再探，
        // 因为 didFinish 时 React 往往还没画完，立刻取 innerText 会是 0——这条日志是
        // 用来排查「窗口白屏」的，必须是可信的就绪信号。
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { [weak self] in
            guard self != nil, let view = self?.webView else { return }
            let probe =
                "document.title + '\u{1}' + document.body.innerText.length + '\u{1}'"
                + " + (document.documentElement.lang || '?')"
            view.evaluateJavaScript(probe) { value, error in
                if let text = value as? String {
                    // 用 components 而不是 split：split 默认会丢掉空字段（title 为空时
                    // 会把正文长度错当成标题），日志就骗人了。
                    let parts = text.components(separatedBy: "\u{1}")
                    Log.line(
                        "内置窗口已渲染：\(url.absoluteString) title=\(parts.first ?? "") "
                            + "正文=\(parts.count > 1 ? parts[1] : "?") 字符 "
                            + "页面语言=\(parts.count > 2 ? parts[2] : "?")")
                } else if let error {
                    Log.line("内置窗口渲染探测失败：\(error.localizedDescription)")
                }
            }
        }
    }
    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        Log.line("内置窗口加载失败：\(error.localizedDescription)")
    }

    func webView(
        _ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!,
        withError error: Error
    ) {
        Log.line("内置窗口连接失败：\(error.localizedDescription)")
    }

    /// 站内链接留在窗口里；住户点开的外部链接（米家授权页、帮助文档）交给默认浏览器，
    /// 免得把第三方登录态混进 App 的 WebView。
    func webView(
        _ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction,
        decisionHandler: @escaping (WKNavigationActionPolicy) -> Void
    ) {
        guard let url = navigationAction.request.url, let scheme = url.scheme?.lowercased() else {
            return decisionHandler(.allow)
        }
        let host = url.host ?? ""
        let internalHost = (host == "127.0.0.1" || host == "localhost")
        let internalScheme = ["about", "blob", "data", "ws", "wss"].contains(scheme)
        if internalHost || internalScheme {
            return decisionHandler(.allow)
        }
        if navigationAction.navigationType == .linkActivated {
            Log.line("内置窗口把外部链接交给浏览器：\(url.absoluteString)")
            NSWorkspace.shared.open(url)
            return decisionHandler(.cancel)
        }
        decisionHandler(.allow)
    }

    /// `target=_blank` / window.open：不在 App 里开新窗口，交给浏览器。
    func webView(
        _ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
        for navigationAction: WKNavigationAction, windowFeatures: WKWindowFeatures
    ) -> WKWebView? {
        if let url = navigationAction.request.url { NSWorkspace.shared.open(url) }
        return nil
    }

    /// 关窗 ≠ 退出：只隐藏，后端继续跑。
    func windowShouldClose(_ sender: NSWindow) -> Bool {
        sender.orderOut(nil)
        return false
    }
}

// MARK: - 防止休眠

/// 服务运行期间阻止**系统空闲休眠**（屏幕该睡还是睡）：系统一睡，米家长连接、摄像头
/// 流与场景联动就全断了。用 NSProcessInfo 的 activity assertion，进程退出自动释放，
/// 不会留下「关不掉的防休眠」。
final class SleepGuard {
    private var token: NSObjectProtocol?

    var isActive: Bool { token != nil }

    func acquire() {
        guard token == nil else { return }
        token = ProcessInfo.processInfo.beginActivity(
            options: [.idleSystemSleepDisabled, .userInitiated],
            reason: "Miloco 服务运行中：保持米家连接与摄像头画面")
        Log.line("已开启「运行期间防止系统休眠」")
    }

    func release() {
        guard let token else { return }
        ProcessInfo.processInfo.endActivity(token)
        self.token = nil
        Log.line("已释放「运行期间防止系统休眠」")
    }
}

// MARK: - AppDelegate

final class AppDelegate: NSObject, NSApplicationDelegate {
    private var statusItem: NSStatusItem?
    private let backend = BackendProcess()
    private var healthTimer: Timer?
    private var healthFailures = 0
    private var consecutiveRestarts = 0
    private var healthySince: Date?
    private var isQuitting = false
    /// 状态文本存英/中一对，读取时才按当前语言渲染 —— 这样切语言后状态栏文案
    /// 不用等下一次状态变化才更新。
    private var statePair: (en: String, zh: String) = ("Stopped", "已停止")
    private var stateText: String { t(statePair.en, statePair.zh) }

    private func setState(_ en: String, _ zh: String) { statePair = (en, zh) }
    /// 信号源必须持有强引用，否则会被立刻回收、信号无人处理。
    private var signalSources: [DispatchSourceSignal] = []
    /// 本 App 自带的管理页面窗口（WKWebView）。
    private let adminWindow = AdminWindow()
    /// 运行期间阻止系统休眠。
    private let sleepGuard = SleepGuard()
    /// 接管他人后端时它所在的端口（backend.isRunning 为 false，端口得单独记）。
    private var adoptedPort: Int?
    /// 住户在服务就绪前就点了「打开管理页面」→ 就绪后自动把页面放出来。
    private var pendingOpenPage = false

    /// 菜单「运行期间防止休眠」，默认开。
    private var preventSleep: Bool {
        get {
            UserDefaults.standard.object(forKey: "preventSleep") == nil
                ? true : UserDefaults.standard.bool(forKey: "preventSleep")
        }
        set { UserDefaults.standard.set(newValue, forKey: "preventSleep") }
    }

    /// 后端在跑（自己起的或接管的）时才需要拦住休眠。
    private var isServiceUp: Bool { backend.isRunning || adoptedPort != nil }

    private func currentPort() -> Int? {
        if backend.isRunning { return backend.port }
        return adoptedPort
    }

    /// 让防休眠断言跟着服务状态走：服务在跑就拦住系统休眠，停了/用户关了就释放。
    private func syncSleepGuard() {
        if preventSleep && isServiceUp {
            sleepGuard.acquire()
        } else {
            sleepGuard.release()
        }
    }

    // MARK: 生命周期

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        DistributedNotificationCenter.default().addObserver(
            self, selector: #selector(handleShowPageNotification),
            name: kShowPageNotification, object: nil)

        // 单实例：已有同类 App 在跑 → 通知它打开页面，自己退出。
        let others = NSRunningApplication
            .runningApplications(withBundleIdentifier: kBundleID)
            .filter { $0.processIdentifier != ProcessInfo.processInfo.processIdentifier }
        if !others.isEmpty {
            Log.line("检测到已有实例在运行，转发“打开管理页面”后退出")
            DistributedNotificationCenter.default().postNotificationName(
                kShowPageNotification, object: nil, userInfo: nil,
                deliverImmediately: true)
            NSApp.terminate(nil)
            return
        }

        Log.line("界面语言：\(AppLanguage.code)（系统首选：\(Locale.preferredLanguages.first ?? "?")）")
        prepareHome()
        syncAutostartPath()
        installSignalHandlers()
        // 页面里切语言 → 原生菜单跟着切（同一个 App 里两边语言保持一致）。
        adminWindow.onLanguageReported = { [weak self] lang in
            guard let self else { return }
            guard lang != AppLanguage.code else {
                Log.line("页面回报语言 \(lang)，与界面语言一致")
                return
            }
            Log.line("页面请求把界面语言切到 \(lang)")
            AppLanguage.override = lang
            self.applyLanguageChange()
        }
        buildMenu()
        // 双击图标就立刻把窗口亮出来并抢焦点（先占位，后端就绪后自动换成真页面），
        // 别让人对着桌面等后端 bootstrap。
        pendingOpenPage = true
        adminWindow.showPlaceholder(t("Miloco is starting…", "Miloco 正在启动…"))
        startServer(openPageWhenReady: true)
        startHealthTimer()
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        openAdminPage()
        return true
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        isQuitting = true
        healthTimer?.invalidate()
        sleepGuard.release()
        setState("Quitting…", "正在退出…")
        refreshMenu()
        DispatchQueue.global().async { [weak self] in
            self?.backend.stop()
            DispatchQueue.main.async {
                NSApp.reply(toApplicationShouldTerminate: true)
                // 保底：个别场景（从 SIGTERM 触发的终止）AppKit 会吞掉 terminateLater，
                // 结果是后端停干净了、应用却继续活着。3s 后仍在就直接退出——此时后端
                // 已停，不会留下孤儿进程占端口。
                DispatchQueue.main.asyncAfter(deadline: .now() + 3) {
                    Log.line("终止流程未在 3s 内完成，强制退出")
                    exit(0)
                }
            }
        }
        return .terminateLater
    }

    // MARK: 首次运行准备

    /// 首启准备：只建自己的目录、写自己的默认配置。
    ///
    /// **不读取、不复制任何外部安装的数据。** 早期版本会自动导入 `~/.openclaw/miloco`
    /// 的 config.json / miloco.db，结果是 App 悄悄继承了 CLI 版的米家账号、模型 API Key
    /// 与规则库 —— 那既不是「独立应用」该有的行为，也让住户无法判断 App 到底在用谁的数据。
    /// 现在 App 的数据**只**在 AppPaths.home（~/Library/Application Support/Miloco）里，
    /// 与任何 CLI / openclaw 安装完全隔离，卸载/重装互不影响。
    private func prepareHome() {
        let fm = FileManager.default
        try? fm.createDirectory(at: AppPaths.home, withIntermediateDirectories: true)
        try? fm.createDirectory(at: AppPaths.logDir, withIntermediateDirectories: true)

        if !fm.fileExists(atPath: AppPaths.configFile.path) {
            guard fm.fileExists(atPath: AppPaths.defaultsConfig.path) else {
                Log.line("包内默认配置缺失，跳过首启初始化")
                return
            }
            try? fm.copyItem(at: AppPaths.defaultsConfig, to: AppPaths.configFile)
            Log.line("首启：已写入包内默认 config.json")
        }
    }

    /// 兜底退出路径：SIGTERM/SIGINT/SIGHUP（登出、关机、`kill`）**不会**触发
    /// applicationShouldTerminate；不接管的话启动器直接死掉，后端子进程会变成孤儿继续
    /// 占着 1810 端口。这里把信号转成正常退出流程，保证子进程一定被收掉。
    private func installSignalHandlers() {
        for sig in [SIGTERM, SIGINT, SIGHUP] {
            let source = DispatchSource.makeSignalSource(signal: sig, queue: .main)
            source.setEventHandler { [weak self] in
                // ⚠️ 不能在信号回调里调 NSApp.terminate：terminateLater 会等
                // reply(toApplicationShouldTerminate:)，而那个 reply 要排队进主队列，
                // 主队列此刻正被本回调占着 → 互相等待。实测表现是「后端已经停掉、
                // 应用却一直不退出」。这里直接停后端 + exit，路径最短且不留孤儿进程。
                Log.line("收到信号 \(sig)：停止后端并退出")
                self?.backend.stop()
                exit(0)
            }
            source.resume()
            signal(sig, SIG_IGN) // 默认动作改为忽略，交给上面的 source
            signalSources.append(source)
        }
    }

    /// 用内置解释器按后端的真实加载路径校验 config.json（JSON 语法 + pydantic 校验）。
    /// 后端 `miloco.main` 在 import 期就会 `get_settings()`，配置坏了整个服务起不来；
    /// 消费级应用不能把住户卡在“反复启动失败”上，所以启动前先验，坏了就备份 + 回默认。
    private func configIsValid() -> Bool {
        guard FileManager.default.fileExists(atPath: AppPaths.configFile.path) else {
            return true // 没有配置文件 = 走包内默认值，不算坏
        }
        let task = Process()
        task.executableURL = AppPaths.python
        task.arguments = ["-c", "from miloco.config import get_settings; get_settings()"]
        var env = ProcessInfo.processInfo.environment
        env["MILOCO_HOME"] = AppPaths.home.path
        env["MILOCO_EDITION"] = "slim"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        task.environment = env
        task.standardOutput = FileHandle.nullDevice
        task.standardError = FileHandle.nullDevice
        do {
            try task.run()
        } catch {
            return true // 解释器本身起不来：不拦，让后端把真实错误打进日志
        }
        task.waitUntilExit()
        return task.terminationStatus == 0
    }

    /// 配置损坏时的自愈：把坏配置另存为 config.json.bad-<时间戳>（住户可自行回查），
    /// 再用包内默认配置重建，绝不静默删除用户数据。
    private func recoverBrokenConfig() {
        let fm = FileManager.default
        let stamp = Int(Date().timeIntervalSince1970)
        let backup = AppPaths.home.appendingPathComponent("config.json.bad-\(stamp)")
        do {
            try fm.moveItem(at: AppPaths.configFile, to: backup)
            Log.line("config.json 校验失败，已备份为 \(backup.lastPathComponent)")
        } catch {
            Log.line("备份坏配置失败：\(error)")
        }
        if fm.fileExists(atPath: AppPaths.defaultsConfig.path) {
            try? fm.copyItem(at: AppPaths.defaultsConfig, to: AppPaths.configFile)
            Log.line("已用包内默认配置重建 config.json")
        }
        notify(
            t("Miloco config was reset", "Miloco 配置已重置"),
            t(
                "The old config could not be parsed; backed up as \(backup.lastPathComponent)",
                "原配置无法解析，已备份为 \(backup.lastPathComponent)"))
    }

    // MARK: 菜单

    private func buildMenu() {
        let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        if let button = item.button {
            button.image = statusBarIcon()
            button.imageScaling = .scaleProportionallyDown
            button.toolTip = "Miloco"
        }
        statusItem = item
        buildMainMenu()
        refreshMenu()
    }

    /// 菜单栏图标就用 App 自己的图标（和停靠栏、Finder 里一致），不是 Symbol 摄像头。
    /// 取包内 Miloco.icns；直接跑裸二进制（开发态）时退回 NSApp.applicationIconImage。
    /// isTemplate 必须关掉，否则彩色图标会被系统压成单色剪影。
    private func statusBarIcon() -> NSImage? {
        // 菜单栏图标 = favicon 里那栋房子、去掉橙色底、单色模板。
        // 为什么不能用 App 图标（Miloco.icns）缩一缩：icns 是 QuickLook 渲的，**背景不透明**
        // （白底），缩到 18px 放菜单栏就是一块带白边的方块；而模板图走 alpha 通道，白底会
        // 整块变成实心方块。这里直接载入 SVG：AppKit 会用 _NSSVGImageRep 矢量渲染，
        // 透明度正确、任意分辨率都清晰。
        if let url = Bundle.main.url(forResource: "MenuBarIcon", withExtension: "svg"),
            let image = NSImage(contentsOf: url)
        {
            image.size = NSSize(width: 18, height: 18)
            image.isTemplate = true
            Log.line("菜单栏图标：MenuBarIcon.svg（房子线稿·模板单色 isTemplate=\(image.isTemplate)）")
            return image
        }
        var base: NSImage?
        if let url = Bundle.main.url(forResource: "Miloco", withExtension: "icns") {
            base = NSImage(contentsOf: url)
        }
        if base == nil { base = NSApp.applicationIconImage }
        guard let image = base?.copy() as? NSImage else { return nil }
        image.size = NSSize(width: 18, height: 18)
        image.isTemplate = false
        Log.line("菜单栏图标：退回 App 图标（资源里缺 MenuBarIcon.svg）")
        return image
    }

    /// 菜单栏图标自检：把资源里的 SVG 渲到 36x36，统计透明/不透明像素。
    /// 模板图必须**有透明像素**（否则整块实心、就是白边方块的来源），也要有实心像素
    /// （否则图是空的）。构建期由 smoke_test.sh 断言，避免以后又退回不透明图标。
    static func menuBarIconCheck() -> String {
        guard let url = Bundle.main.url(forResource: "MenuBarIcon", withExtension: "svg"),
            let image = NSImage(contentsOf: url)
        else { return "missing" }
        let side = 36
        guard
            let rep = NSBitmapImageRep(
                bitmapDataPlanes: nil, pixelsWide: side, pixelsHigh: side, bitsPerSample: 8,
                samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB,
                bytesPerRow: 0, bitsPerPixel: 0)
        else { return "norep" }
        NSGraphicsContext.saveGraphicsState()
        NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
        image.draw(in: NSRect(x: 0, y: 0, width: side, height: side))
        NSGraphicsContext.restoreGraphicsState()
        var transparent = 0
        var solid = 0
        for y in 0..<side {
            for x in 0..<side {
                let alpha = Double(rep.colorAt(x: x, y: y)?.alphaComponent ?? 1)
                if alpha < 0.1 { transparent += 1 } else if alpha > 0.5 { solid += 1 }
            }
        }
        return "svg(transparent=\(transparent),solid=\(solid))"
    }

    /// AppKit 主菜单 —— **不是装饰**。WKWebView 的 ⌘C/⌘V/⌘A/⌘Z 是「主菜单的 key
    /// equivalent 命中 → 转发给 first responder」这条路，没有主菜单时在页面里按 ⌘V
    /// 什么都不发生（右键 → 粘贴仍然可用），表现就是「粘贴 token / 模型配置失败」。
    private func buildMainMenu() {
        let main = NSMenu()

        let appItem = NSMenuItem()
        main.addItem(appItem)
        let appMenu = NSMenu(title: "Miloco")
        addMenuItem(appMenu, t("About Miloco", "关于 Miloco"), #selector(NSApplication.orderFrontStandardAboutPanel(_:)), "")
        appMenu.addItem(.separator())
        addMenuItem(appMenu, t("Hide Miloco", "隐藏 Miloco"), #selector(NSApplication.hide(_:)), "h")
        addMenuItem(appMenu, t("Quit Miloco", "退出 Miloco"), #selector(quit), "q", target: self)
        appMenu.addItem(.separator())
        let langItem = NSMenuItem(title: t("Language", "语言"), action: nil, keyEquivalent: "")
        langItem.submenu = makeLanguageMenu()
        appMenu.addItem(langItem)
        appItem.submenu = appMenu

        let editItem = NSMenuItem()
        main.addItem(editItem)
        let editMenu = NSMenu(title: t("Edit", "编辑"))
        addMenuItem(editMenu, t("Undo", "撤销"), NSSelectorFromString("undo:"), "z")
        addMenuItem(editMenu, t("Redo", "重做"), NSSelectorFromString("redo:"), "Z")
        editMenu.addItem(.separator())
        addMenuItem(editMenu, t("Cut", "剪切"), #selector(NSText.cut(_:)), "x")
        addMenuItem(editMenu, t("Copy", "拷贝"), #selector(NSText.copy(_:)), "c")
        addMenuItem(editMenu, t("Paste", "粘贴"), #selector(NSText.paste(_:)), "v")
        addMenuItem(editMenu, t("Select All", "全选"), #selector(NSText.selectAll(_:)), "a")
        editItem.submenu = editMenu

        let windowItem = NSMenuItem()
        main.addItem(windowItem)
        let windowMenu = NSMenu(title: t("Window", "窗口"))
        addMenuItem(windowMenu, t("Minimize", "最小化"), #selector(NSWindow.performMiniaturize(_:)), "m")
        addMenuItem(windowMenu, t("Zoom", "缩放"), #selector(NSWindow.performZoom(_:)), "")
        windowMenu.addItem(.separator())
        addMenuItem(windowMenu, t("Close Window", "关闭窗口"), #selector(NSWindow.performClose(_:)), "w")
        addMenuItem(windowMenu, t("Bring All to Front", "前置全部窗口"), #selector(NSApplication.arrangeInFront(_:)), "")
        windowItem.submenu = windowMenu
        NSApp.windowsMenu = windowMenu

        NSApp.mainMenu = main
        // 把主菜单结构记进日志：粘贴/复制这类快捷键是「主菜单命中 → 转发 first
        // responder」，出问题时这一行能直接看出菜单有没有缺项。
        let summary = main.items.compactMap { item -> String? in
            guard let sub = item.submenu else { return nil }
            // isAlternate 是 AppKit 给同一个动作挂的隐藏备用键（如 Emoji & Symbols
            // 的 ⌃⌘Space / ⌘fnE），列出来会像是重复项，菜单里其实是同一个。
            let keys = sub.items.filter { !$0.keyEquivalent.isEmpty && !$0.isAlternate }
                .map { "\($0.title)⌘\($0.keyEquivalent.uppercased())" }
            return "\(sub.title)[\(keys.joined(separator: " "))]"
        }.joined(separator: " ")
        Log.line("主菜单：\(summary)")
    }

    /// 主菜单项默认 target=nil（走 responder chain，交给当前 first responder）；
    /// 只有 App 菜单里那几项需要显式指到 self。
    private func addMenuItem(
        _ menu: NSMenu, _ title: String, _ action: Selector, _ key: String, target: AnyObject? = nil
    ) {
        let item = NSMenuItem(title: title, action: action, keyEquivalent: key)
        item.target = target
        menu.addItem(item)
    }

    /// 「语言」子菜单：跟随系统 / 简体中文 / English。选了之后原生菜单与页面一起切。
    private func makeLanguageMenu() -> NSMenu {
        let menu = NSMenu(title: t("Language", "语言"))
        let current = AppLanguage.menuSelection
        for (value, title) in [
            ("system", t("Follow System", "跟随系统")),
            ("zh", "简体中文"),
            ("en", "English"),
        ] {
            let item = NSMenuItem(title: title, action: #selector(setLanguage(_:)), keyEquivalent: "")
            item.target = self
            item.representedObject = value
            item.state = (current == value) ? .on : .off
            menu.addItem(item)
        }
        return menu
    }

    @objc private func setLanguage(_ sender: NSMenuItem) {
        AppLanguage.override = (sender.representedObject as? String) ?? "system"
        let backToSystem = AppLanguage.menuSelection == "system"
        applyLanguageChange(autoLanguage: backToSystem)
        if backToSystem {
            Log.line("已回到「跟随系统」，并让页面清掉自身语言偏好")
        }
    }

    /// 切语言：重建两套菜单 + 让页面也切过去（重新加载并带上 ?lang=）。
    private func applyLanguageChange(autoLanguage: Bool = false) {
        Log.line("界面语言切换为 \(AppLanguage.code)（选择：\(AppLanguage.menuSelection)）")
        buildMainMenu()
        refreshMenu()
        guard adminWindow.isVisible, let url = adminURL(autoLanguage: autoLanguage) else { return }
        adminWindow.reload(url: url)
    }

    private func refreshMenu() {
        let menu = NSMenu()
        let version = Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "?"
        let header = NSMenuItem(title: "Miloco \(version) · \(stateText)", action: nil, keyEquivalent: "")
        header.isEnabled = false
        menu.addItem(header)
        menu.addItem(.separator())

        let open = NSMenuItem(
            title: t("Open Dashboard", "打开管理页面"), action: #selector(openAdminPage),
            keyEquivalent: "o")
        open.target = self
        menu.addItem(open)

        let openBrowser = NSMenuItem(
            title: t("Open in Browser", "在浏览器中打开"), action: #selector(openAdminInBrowser),
            keyEquivalent: "")
        openBrowser.target = self
        menu.addItem(openBrowser)
        menu.addItem(.separator())

        let running = backend.isRunning
        let start = NSMenuItem(
            title: t("Start Service", "启动服务"), action: #selector(startService), keyEquivalent: "s")
        start.target = self
        start.isEnabled = !running
        menu.addItem(start)

        let stop = NSMenuItem(
            title: t("Stop Service", "停止服务"), action: #selector(stopService), keyEquivalent: ".")
        stop.target = self
        stop.isEnabled = running
        menu.addItem(stop)

        let restart = NSMenuItem(
            title: t("Restart Service", "重启服务"), action: #selector(restartService),
            keyEquivalent: "r")
        restart.target = self
        restart.isEnabled = running
        menu.addItem(restart)
        menu.addItem(.separator())

        let autostart = NSMenuItem(
            title: t("Launch at Login", "开机自动启动"), action: #selector(toggleAutostart),
            keyEquivalent: "")
        autostart.target = self
        autostart.state = FileManager.default.fileExists(atPath: AppPaths.launchAgentPlist.path)
            ? .on : .off
        menu.addItem(autostart)

        let noSleep = NSMenuItem(
            title: t("Prevent Sleep While Running", "运行期间防止休眠"),
            action: #selector(togglePreventSleep), keyEquivalent: "")
        noSleep.target = self
        noSleep.state = preventSleep ? .on : .off
        menu.addItem(noSleep)

        let language = NSMenuItem(title: t("Language", "语言"), action: nil, keyEquivalent: "")
        language.submenu = makeLanguageMenu()
        menu.addItem(language)

        let openHome = NSMenuItem(
            title: t("Open Data Folder", "打开数据目录"), action: #selector(openHome),
            keyEquivalent: "")
        openHome.target = self
        menu.addItem(openHome)

        let openLogs = NSMenuItem(
            title: t("Open Logs", "打开日志目录"), action: #selector(openLogs), keyEquivalent: "l")
        openLogs.target = self
        menu.addItem(openLogs)
        menu.addItem(.separator())

        let quit = NSMenuItem(
            title: t("Quit Miloco", "退出 Miloco"), action: #selector(quit), keyEquivalent: "q")
        quit.target = self
        menu.addItem(quit)

        statusItem?.menu = menu
    }

    // MARK: 服务控制

    private func startServer(openPageWhenReady: Bool) {
        if let port = BackendProcess.findRunningPort() {
            if isOurBackend(port: port) {
                Log.line("端口 \(port) 上已有本 App 的后端在运行，接管为运行中")
                if port != kPreferredPort {
                    Log.line("注意：本 App 的服务端口是 \(port)（固定端口 \(kPreferredPort) 当时被别的程序占用）")
                }
            } else {
                // 不是本数据目录的后端（token 不匹配）。仍复用它：同一台机器跑两套感知引擎会
                // 重复占用米家账号配额、抢摄像头流，比"看到别人的数据"更糟。但要明确告知。
                let msg = t(
                    "The Miloco on port \(port) was not started by this app (different data folder);"
                        + " reusing it. If the page does not show this app's data, quit that backend"
                        + " and restart this app",
                    "端口 \(port) 上的 Miloco 不是本 App 启动的（数据目录不同），已复用它运行；"
                        + "页面里若看到的不是本 App 的数据，请先退出那个后端再重启本 App")
                Log.line(msg)
                notify(t("Miloco is reusing a running backend", "Miloco 复用了已运行的后端"), msg)
            }
            adoptedPort = port
            setState("Running (127.0.0.1:\(port))", "运行中（127.0.0.1:\(port)）")
            syncSleepGuard()
            refreshMenu()
            if openPageWhenReady { openAdminPage() }
            return
        }
        guard let port = BackendProcess.pickPort() else {
            setState(
                "Cannot start: ports \(kPortRange.lowerBound)-\(kPortRange.upperBound) are all in use",
                "无法启动：\(kPortRange.lowerBound)-\(kPortRange.upperBound) 端口全被占用")
            Log.line(stateText)
            refreshMenu()
            notify(t("Miloco cannot start", "Miloco 无法启动"), stateText)
            return
        }
        setState("Starting…", "启动中…")
        refreshMenu()
        // 启动前自检配置：损坏就自愈（备份 + 回默认），否则后端会在 import 期直接退出，
        // 住户只会看到“服务反复异常退出”而不知道原因。
        if !configIsValid() {
            recoverBrokenConfig()
        }
        backend.onUnexpectedExit = { [weak self] code in
            self?.handleUnexpectedExit(code: code)
        }
        do {
            try backend.start(port: port)
        } catch {
            setState(
                "Start failed: \(error.localizedDescription)",
                "启动失败：\(error.localizedDescription)")
            Log.line("启动失败：\(error)")
            refreshMenu()
            notify(t("Miloco failed to start", "Miloco 启动失败"), error.localizedDescription)
            return
        }
        waitUntilReady(port: port, openPage: openPageWhenReady)
    }

    private func waitUntilReady(port: Int, openPage: Bool) {
        DispatchQueue.global().async { [weak self] in
            let deadline = Date().addingTimeInterval(kStartupTimeout)
            while Date() < deadline {
                if isMilocoHealthy(port: port) {
                    DispatchQueue.main.async {
                        guard let self else { return }
                        self.adoptedPort = nil
                        self.setState(
                            "Running (127.0.0.1:\(port))", "运行中（127.0.0.1:\(port)）")
                        self.healthFailures = 0
                        self.healthySince = Date()
                        self.syncSleepGuard()
                        self.refreshMenu()
                        Log.line("后端就绪：http://127.0.0.1:\(port)/")
                        if openPage { self.openAdminPage() }
                    }
                    return
                }
                if self?.backend.isRunning == false { return }
                usleep(500_000)
            }
            DispatchQueue.main.async {
                guard let self else { return }
                self.setState("Start timed out (see logs)", "启动超时（详见日志）")
                self.refreshMenu()
                Log.line("后端在 \(Int(kStartupTimeout))s 内未就绪")
                self.notify(
                    t("Miloco start timed out", "Miloco 启动超时"),
                    t(
                        "Please check the logs under \(AppPaths.logDir.path)",
                        "请查看 \(AppPaths.logDir.path) 下的日志"))
            }
        }
    }

    private func handleUnexpectedExit(code: Int32) {
        guard !isQuitting else { return }
        consecutiveRestarts += 1
        if consecutiveRestarts > kMaxConsecutiveRestarts {
            setState(
                "Service keeps crashing; auto-restart stopped",
                "服务反复异常退出，已停止自动重启")
            refreshMenu()
            Log.line("连续异常退出 \(consecutiveRestarts) 次，停止自动重启")
            syncSleepGuard()
            notify(
                t("Miloco service error", "Miloco 服务异常"),
                t(
                    "Crashed \(consecutiveRestarts) times in a row; please check the logs",
                    "已连续异常退出 \(consecutiveRestarts) 次，请查看日志"))
            return
        }
        let delay = min(30.0, pow(2.0, Double(consecutiveRestarts - 1)))
        setState(
            "Crashed (exit=\(code)); restarting in \(Int(delay))s",
            "异常退出（exit=\(code)），\(Int(delay))s 后重启")
        refreshMenu()
        DispatchQueue.main.asyncAfter(deadline: .now() + delay) { [weak self] in
            guard let self, !self.isQuitting, !self.backend.isRunning else { return }
            Log.line("自动重启服务（第 \(self.consecutiveRestarts) 次）")
            self.startServer(openPageWhenReady: false)
        }
    }

    private func startHealthTimer() {
        healthTimer?.invalidate()
        healthTimer = Timer.scheduledTimer(withTimeInterval: kHealthCheckInterval, repeats: true) {
            [weak self] _ in
            self?.healthCheck()
        }
    }

    /// 健康巡检：连续失败达阈值即视为“卡死/僵死”，主动重启。
    private func healthCheck() {
        guard backend.isRunning, !isQuitting else { return }
        let port = backend.port
        DispatchQueue.global().async { [weak self] in
            let ok = isMilocoHealthy(port: port)
            DispatchQueue.main.async {
                guard let self else { return }
                if ok {
                    self.healthFailures = 0
                    if let since = self.healthySince, Date().timeIntervalSince(since) > kHealthyResetInterval {
                        self.consecutiveRestarts = 0
                    }
                    return
                }
                self.healthFailures += 1
                Log.line("健康检查失败 \(self.healthFailures)/\(kHealthFailuresBeforeRestart)")
                if self.healthFailures >= kHealthFailuresBeforeRestart {
                    self.healthFailures = 0
                    Log.line("服务无响应，判定为异常，执行重启")
                    self.stopService()
                    self.startServer(openPageWhenReady: false)
                }
            }
        }
    }

    // MARK: 菜单动作

    /// 管理页地址：优先用已知端口，未知再探一次。
    /// `autoLanguage: true` 表示「住户刚在菜单里选了跟随系统」：除了不带 ?lang=，
    /// 还要显式让页面清掉它自己存过的语言偏好（?lang=auto），否则页面会把自己存的
    /// 语言回报给原生，把「跟随系统」立刻顶回去。
    private func adminURL(autoLanguage: Bool = false) -> URL? {
        let port = currentPort() ?? BackendProcess.findRunningPort() ?? kPreferredPort
        let query: String
        if autoLanguage {
            query = "?lang=auto"
        } else {
            // 原生端选了明确语言（菜单「语言」或 MILOCO_APP_LANG）时把选择带给页面；
            // 「跟随系统」就不带，让页面跟随系统 / 页面里存过的偏好。
            query = AppLanguage.menuSelection == "system" ? "" : "?lang=\(AppLanguage.code)"
        }
        return URL(string: "http://127.0.0.1:\(port)/\(query)")
    }

    /// 默认行为：在 App 自带的窗口里打开（像 Electron/Tauri 那样，不跳浏览器）。
    @objc private func openAdminPage() {
        guard let url = adminURL() else { return }
        let port = url.port ?? kPreferredPort
        if isMilocoHealthy(port: port) {
            pendingOpenPage = false
            adminWindow.show(url: url)
        } else {
            // 后端还在启动：先给个占位，就绪后自动换成真页面。
            pendingOpenPage = true
            adminWindow.showPlaceholder(t("Miloco is starting…", "Miloco 正在启动…"))
        }
    }

    /// 想用浏览器（多屏、调试、投屏）时走这里。
    @objc private func openAdminInBrowser() {
        guard let url = adminURL() else { return }
        Log.line("在浏览器中打开管理页面 \(url.absoluteString)")
        NSWorkspace.shared.open(url)
    }

    @objc private func handleShowPageNotification() {
        openAdminPage()
    }

    @objc private func togglePreventSleep() {
        preventSleep = !preventSleep
        syncSleepGuard()
        refreshMenu()
    }

    @objc private func startService() {
        consecutiveRestarts = 0
        startServer(openPageWhenReady: true)
    }

    @objc private func stopService() {
        backend.stop()
        adoptedPort = nil
        setState("Stopped", "已停止")
        syncSleepGuard()
        refreshMenu()
    }

    @objc private func restartService() {
        setState("Restarting…", "重启中…")
        refreshMenu()
        DispatchQueue.global().async { [weak self] in
            guard let self else { return }
            self.backend.stop()
            DispatchQueue.main.async {
                self.adoptedPort = nil
                self.consecutiveRestarts = 0
                self.startServer(openPageWhenReady: false)
            }
        }
    }

    @objc private func toggleAutostart() {
        let fm = FileManager.default
        if fm.fileExists(atPath: AppPaths.launchAgentPlist.path) {
            try? fm.removeItem(at: AppPaths.launchAgentPlist)
            Log.line("已关闭开机自启")
        } else {
            writeAutostartPlist()
        }
        refreshMenu()
    }

    /// 写 `~/Library/LaunchAgents/<bundleid>.plist`（开机自启）。
    /// ProgramArguments 必须是**绝对路径**，所以 App 换位置后要重写，见 syncAutostartPath()。
    private func writeAutostartPlist() {
        let fm = FileManager.default
        let executable = Bundle.main.executableURL?.path ?? CommandLine.arguments[0]
        let plist = """
        <?xml version="1.0" encoding="UTF-8"?>
        <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
        <plist version="1.0">
        <dict>
          <key>Label</key><string>\(kBundleID)</string>
          <key>ProgramArguments</key><array><string>\(executable)</string></array>
          <key>RunAtLoad</key><true/>
          <key>ProcessType</key><string>Interactive</string>
        </dict>
        </plist>
        """
        try? fm.createDirectory(
            at: AppPaths.launchAgentPlist.deletingLastPathComponent(),
            withIntermediateDirectories: true)
        do {
            try plist.write(to: AppPaths.launchAgentPlist, atomically: true, encoding: .utf8)
            Log.line("已开启开机自启：\(AppPaths.launchAgentPlist.path)")
        } catch {
            Log.line("写入 LaunchAgent 失败：\(error)")
        }
    }

    /// 已经开了自启、但 App 被挪到了别处（下载目录 → /Applications 是常态）：按当前
    /// 路径重写，否则开机时 launchd 去启动一个已不存在的可执行文件，住户只会看到
    /// "开机自启失效"，且没有任何提示。
    private func syncAutostartPath() {
        guard FileManager.default.fileExists(atPath: AppPaths.launchAgentPlist.path),
            let text = try? String(contentsOf: AppPaths.launchAgentPlist, encoding: .utf8)
        else { return }
        let executable = Bundle.main.executableURL?.path ?? CommandLine.arguments[0]
        guard !text.contains("<string>\(executable)</string>") else { return }
        writeAutostartPlist()
        Log.line("App 位置变化，已更新开机自启路径：\(executable)")
    }

    @objc private func openHome() {
        NSWorkspace.shared.open(AppPaths.home)
    }

    @objc private func openLogs() {
        NSWorkspace.shared.open(AppPaths.logDir)
    }

    @objc private func quit() {
        isQuitting = true
        NSApp.terminate(nil)
    }

    private func notify(_ title: String, _ body: String) {
        let script = "display notification \"\(body.replacingOccurrences(of: "\"", with: "'"))\" with title \"\(title)\""
        let task = Process()
        task.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
        task.arguments = ["-e", script]
        try? task.run()
    }
}

// MARK: - 入口

if CommandLine.arguments.contains("--selftest") {
    // 构建期冒烟自检：不启 UI、不启服务，只验证打包布局与端口探测可用。
    let fm = FileManager.default
    var problems: [String] = []
    if !fm.isExecutableFile(atPath: AppPaths.python.path) {
        problems.append("内置解释器缺失或不可执行：\(AppPaths.python.path)")
    }
    if !fm.fileExists(atPath: AppPaths.defaultsConfig.path) {
        problems.append("默认配置缺失：\(AppPaths.defaultsConfig.path)")
    }
    if BackendProcess.pickPort() == nil {
        problems.append("端口 \(kPortRange) 全部被占用")
    }
    if problems.isEmpty {
        // 把字段拆成数组再 join：一整条字符串拼接会让 Swift 的类型检查卡住（实测超时）。
        // lang/langquery/menubar 让「界面语言默认英文 + 跟随系统」和「菜单栏是透明模板图」
        // 都能在构建期断言，而不是只能靠肉眼看菜单。
        let langQuery = AppLanguage.menuSelection == "system" ? "auto" : AppLanguage.code
        let fields = [
            "LAUNCHER_SELFTEST_OK",
            "python=\(AppPaths.python.path)",
            "home=\(AppPaths.home.path)",
            "port=\(BackendProcess.pickPort() ?? 0)",
            "preferred=\(kPreferredPort)",
            "lang=\(AppLanguage.code)",
            "open=\(t("Open Dashboard", "打开管理页面"))",
            "quit=\(t("Quit Miloco", "退出 Miloco"))",
            "langquery=\(langQuery)",
            "menubar=\(AppDelegate.menuBarIconCheck())",
        ]
        print(fields.joined(separator: " "))
        exit(0)
    }
    for problem in problems { print("SELFTEST_FAIL: \(problem)") }
    exit(1)
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.run()
