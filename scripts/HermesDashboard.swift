import Foundation
import Dispatch
import Darwin

final class DashboardSupervisor {
    private let hermes = "/Users/diatche/.hermes/hermes-agent/venv/bin/hermes"
    private let timeoutBudgetScript = "/Users/diatche/.hermes/hermes-agent/scripts/hermes-wrapper-timeout-budget.py"
    private let configReadTimeout: TimeInterval = 5
    private let lock = NSLock()
    private var child: Process?
    private var childHasDedicatedGroup = false
    private var shutdownGrace: TimeInterval = 0
    private var shuttingDown = false

    private func log(_ message: String) {
        let formatter = ISO8601DateFormatter()
        FileHandle.standardError.write(Data("[\(formatter.string(from: Date()))] \(message)\n".utf8))
    }

    private func configuredEnvironment() -> [String: String] {
        var env = ProcessInfo.processInfo.environment
        let hermesRoot = "/Users/diatche/.hermes/hermes-agent"
        let venv = "\(hermesRoot)/venv"
        env["HERMES_HOME"] = "/Users/diatche/.hermes"
        env["VIRTUAL_ENV"] = venv
        let prefix = [
            "\(venv)/bin",
            "\(hermesRoot)/node_modules/.bin",
            "/Users/diatche/.nvm/versions/node/v24.14.0/bin",
            "/Users/diatche/.local/bin",
            "/opt/homebrew/bin",
            "/opt/homebrew/sbin",
            "/usr/local/bin",
            "/usr/bin",
            "/bin",
            "/usr/sbin",
            "/sbin"
        ].joined(separator: ":")
        env["PATH"] = prefix + ":" + (env["PATH"] ?? "")
        return env
    }

    private func readShutdownGrace() throws -> TimeInterval {
        let process = Process()
        let output = Pipe()
        process.executableURL = URL(fileURLWithPath: timeoutBudgetScript)
        process.arguments = ["--json"]
        process.currentDirectoryURL = URL(fileURLWithPath: "/Users/diatche/.hermes")
        process.environment = configuredEnvironment()
        process.standardOutput = output
        process.standardError = FileHandle.standardError
        try process.run()

        let deadline = ProcessInfo.processInfo.systemUptime + configReadTimeout
        while process.isRunning && ProcessInfo.processInfo.systemUptime < deadline {
            Thread.sleep(forTimeInterval: 0.02)
        }
        if process.isRunning {
            process.terminate()
            let stopDeadline = ProcessInfo.processInfo.systemUptime + 1
            while process.isRunning && ProcessInfo.processInfo.systemUptime < stopDeadline {
                Thread.sleep(forTimeInterval: 0.02)
            }
            if process.isRunning { kill(process.processIdentifier, SIGKILL) }
            throw NSError(domain: "HermesDashboard", code: 1,
                          userInfo: [NSLocalizedDescriptionKey: "timed out resolving shutdown budget"])
        }
        guard process.terminationStatus == 0 else {
            throw NSError(domain: "HermesDashboard", code: 2,
                          userInfo: [NSLocalizedDescriptionKey: "shutdown budget resolver failed"])
        }
        let data = output.fileHandleForReading.readDataToEndOfFile()
        let object = try JSONSerialization.jsonObject(with: data)
        guard let budgets = object as? [String: Any],
              let grace = budgets["wrapper-grace"] as? NSNumber,
              grace.doubleValue.isFinite, grace.doubleValue >= 0 else {
            throw NSError(domain: "HermesDashboard", code: 3,
                          userInfo: [NSLocalizedDescriptionKey: "invalid shutdown budget response"])
        }
        return grace.doubleValue
    }

    func start() {
        do {
            shutdownGrace = try readShutdownGrace()
        } catch {
            log("refusing to launch dashboard: \(error.localizedDescription)")
            exit(78)
        }

        let process = Process()
        process.executableURL = URL(fileURLWithPath: hermes)
        process.arguments = ["dashboard", "--host", "0.0.0.0", "--port", "9119", "--no-open", "--skip-build"]
        process.currentDirectoryURL = URL(fileURLWithPath: "/Users/diatche/.hermes")
        process.environment = configuredEnvironment()
        process.standardOutput = FileHandle.standardOutput
        process.standardError = FileHandle.standardError
        process.terminationHandler = { [weak self] proc in
            guard let self else { return }
            self.log("dashboard exited status=\(proc.terminationStatus)")
            self.lock.lock()
            let stopping = self.shuttingDown
            self.lock.unlock()
            if !stopping { exit(proc.terminationStatus == 0 ? 1 : proc.terminationStatus) }
        }

        do {
            try process.run()
            let deadline = ProcessInfo.processInfo.systemUptime + 2
            while process.isRunning
                    && getpgid(process.processIdentifier) != process.processIdentifier
                    && ProcessInfo.processInfo.systemUptime < deadline {
                Thread.sleep(forTimeInterval: 0.01)
            }
            child = process
            childHasDedicatedGroup = getpgid(process.processIdentifier) == process.processIdentifier
            guard process.isRunning, childHasDedicatedGroup else {
                log("dashboard failed to establish a dedicated process group")
                shutdownAndWait()
                exit(1)
            }
            log("started dashboard pid=\(process.processIdentifier) shutdown_grace=\(shutdownGrace)")
        } catch {
            log("failed to start dashboard: \(error)")
            exit(1)
        }
    }

    func shutdownAndWait() {
        lock.lock()
        guard !shuttingDown else { lock.unlock(); return }
        shuttingDown = true
        let process = child
        let dedicated = childHasDedicatedGroup
        lock.unlock()

        guard let process, process.isRunning else { return }
        let target = dedicated ? -process.processIdentifier : process.processIdentifier
        if kill(target, SIGTERM) != 0 && errno != ESRCH {
            log("failed to SIGTERM dashboard target=\(target) errno=\(errno)")
        }
        let deadline = ProcessInfo.processInfo.systemUptime + shutdownGrace
        while process.isRunning && ProcessInfo.processInfo.systemUptime < deadline {
            Thread.sleep(forTimeInterval: 0.05)
        }
        if process.isRunning {
            log("force-killing dashboard group after shutdown timeout")
            _ = kill(target, SIGKILL)
        }
    }

    func waitForever() { RunLoop.current.run() }
}

let supervisor = DashboardSupervisor()
signal(SIGTERM, SIG_IGN)
signal(SIGINT, SIG_IGN)
let termSource = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
termSource.setEventHandler { supervisor.shutdownAndWait(); exit(0) }
termSource.resume()
let intSource = DispatchSource.makeSignalSource(signal: SIGINT, queue: .main)
intSource.setEventHandler { supervisor.shutdownAndWait(); exit(0) }
intSource.resume()
supervisor.start()
supervisor.waitForever()
