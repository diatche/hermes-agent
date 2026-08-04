import Foundation
import Dispatch
import Darwin

final class Supervisor {
    private let hermes = "/Users/diatche/.hermes/hermes-agent/venv/bin/hermes"
    private let timeoutBudgetScript = "/Users/diatche/.hermes/hermes-agent/scripts/hermes-wrapper-timeout-budget.py"
    private let timeoutStatePath = "/Users/diatche/.hermes/local/run/hermes-gateway-wrapper-timeouts.json"
    private let configReadTimeout: TimeInterval = 5
    private var shutdownGrace: TimeInterval?
    private var controllerWait: TimeInterval?

    private enum State {
        case running
        case shuttingDown
        case finished
    }

    private struct ManagedChild {
        let process: Process
        let hasDedicatedGroup: Bool
    }

    private let lock = NSLock()
    private var children: [ManagedChild] = []
    private var state = State.running

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
        let pathPrefix = [
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
        env["PATH"] = pathPrefix + ":" + (env["PATH"] ?? "")
        return env
    }

    private func readTimeoutBudgets() throws -> (wrapperGrace: TimeInterval, controllerWait: TimeInterval) {
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
            let termDeadline = ProcessInfo.processInfo.systemUptime + 1
            while process.isRunning && ProcessInfo.processInfo.systemUptime < termDeadline {
                Thread.sleep(forTimeInterval: 0.02)
            }
            if process.isRunning {
                kill(process.processIdentifier, SIGKILL)
                let killDeadline = ProcessInfo.processInfo.systemUptime + 1
                while process.isRunning && ProcessInfo.processInfo.systemUptime < killDeadline {
                    Thread.sleep(forTimeInterval: 0.02)
                }
            }
            if !process.isRunning { process.waitUntilExit() }
            throw NSError(
                domain: "HermesGateway",
                code: 1,
                userInfo: [NSLocalizedDescriptionKey: "timed out resolving wrapper shutdown budget pid=\(process.processIdentifier)"]
            )
        }
        guard process.terminationStatus == 0 else {
            throw NSError(
                domain: "HermesGateway",
                code: 2,
                userInfo: [NSLocalizedDescriptionKey: "timeout budget resolver failed with status \(process.terminationStatus)"]
            )
        }
        let data = output.fileHandleForReading.readDataToEndOfFile()
        let object = try JSONSerialization.jsonObject(with: data)
        guard let budgets = object as? [String: Any],
              let grace = budgets["wrapper-grace"] as? NSNumber,
              let wait = budgets["controller-wait"] as? NSNumber,
              grace.doubleValue.isFinite, grace.doubleValue >= 0,
              wait.doubleValue.isFinite, wait.doubleValue >= grace.doubleValue else {
            throw NSError(
                domain: "HermesGateway",
                code: 3,
                userInfo: [NSLocalizedDescriptionKey: "invalid correlated timeout budget response"]
            )
        }
        return (grace.doubleValue, wait.doubleValue)
    }

    private func publishTimeoutState() throws {
        guard let shutdownGrace, let controllerWait else {
            throw NSError(
                domain: "HermesGateway",
                code: 4,
                userInfo: [NSLocalizedDescriptionKey: "timeout budgets are unavailable"]
            )
        }
        let state: [String: Any] = [
            "pid": Int(getpid()),
            "wrapper_grace": shutdownGrace,
            "controller_wait": controllerWait,
        ]
        let data = try JSONSerialization.data(withJSONObject: state, options: [.sortedKeys])
        let url = URL(fileURLWithPath: timeoutStatePath)
        try FileManager.default.createDirectory(
            at: url.deletingLastPathComponent(),
            withIntermediateDirectories: true
        )
        try data.write(to: url, options: [.atomic])
    }

    private func launch(_ executable: String, _ arguments: [String], name: String) -> Process {
        let process = Process()
        // Launch the managed executable directly. On macOS 26.6, execve-ing it
        // from a /usr/bin/python3 trampoline can leave the replacement image
        // permanently stuck in dyld before main(), so neither child becomes
        // ready even though Foundation still reports the Process as running.
        process.executableURL = URL(fileURLWithPath: executable)
        process.arguments = arguments
        process.currentDirectoryURL = URL(fileURLWithPath: "/Users/diatche/.hermes")
        process.environment = configuredEnvironment()
        process.standardOutput = FileHandle.standardOutput
        process.standardError = FileHandle.standardError
        process.terminationHandler = { [weak self] proc in
            guard let self else { return }
            self.log("\(name) exited status=\(proc.terminationStatus)")
            if self.shutdownAndWait() {
                exit(proc.terminationStatus == 0 ? 0 : proc.terminationStatus)
            }
        }

        // Starting and registering a child is one transaction with respect to
        // shutdown. A termination callback may block here, but cannot snapshot
        // children between process.run() and registration.
        lock.lock()
        guard state == .running else {
            lock.unlock()
            log("refusing to start \(name) after shutdown began")
            dispatchMain()
        }
        do {
            try process.run()
            let groupDeadline = ProcessInfo.processInfo.systemUptime + 2
            while process.isRunning
                && getpgid(process.processIdentifier) != process.processIdentifier
                && ProcessInfo.processInfo.systemUptime < groupDeadline {
                Thread.sleep(forTimeInterval: 0.01)
            }
            let hasDedicatedGroup =
                getpgid(process.processIdentifier) == process.processIdentifier
            if process.isRunning && !hasDedicatedGroup {
                children.append(
                    ManagedChild(process: process, hasDedicatedGroup: false)
                )
                lock.unlock()
                log("failed to establish dedicated process group for \(name)")
                if shutdownAndWait() {
                    exit(1)
                }
                dispatchMain()
            }
            children.append(
                ManagedChild(
                    process: process,
                    hasDedicatedGroup: hasDedicatedGroup
                )
            )
            lock.unlock()
            log("started \(name) pid=\(process.processIdentifier): \(executable) \(arguments.joined(separator: " "))")
        } catch {
            lock.unlock()
            log("failed to start \(name): \(error)")
            if shutdownAndWait() {
                exit(1)
            }
            dispatchMain()
        }
        return process
    }

    func start() {
        do {
            let budgets = try readTimeoutBudgets()
            shutdownGrace = budgets.wrapperGrace
            controllerWait = budgets.controllerWait
            try publishTimeoutState()
            log("shutdown grace configured to \(shutdownGrace!) seconds; controller wait \(controllerWait!) seconds")
        } catch {
            log("refusing to launch children: \(error.localizedDescription)")
            exit(78)
        }
        _ = launch(hermes, ["gateway", "run", "--replace"], name: "gateway")
        _ = launch(hermes, ["dashboard", "--host", "0.0.0.0", "--port", "9119", "--no-open", "--skip-build"], name: "dashboard")
    }

    private func isAlive(_ child: ManagedChild) -> Bool {
        if child.hasDedicatedGroup {
            if kill(-child.process.processIdentifier, 0) == 0 {
                return true
            }
            return errno == EPERM
        }
        return child.process.isRunning
    }

    private func signal(_ child: ManagedChild, _ signal: Int32) {
        let pid = child.process.processIdentifier
        let target = child.hasDedicatedGroup ? -pid : pid
        if kill(target, signal) != 0 && errno != ESRCH {
            log("failed to signal child target=\(target) signal=\(signal) errno=\(errno)")
        }
    }

    @discardableResult
    func shutdownAndWait(timeout: TimeInterval? = nil) -> Bool {
        lock.lock()
        guard state == .running else {
            lock.unlock()
            return false
        }
        state = .shuttingDown
        let snapshot = children
        lock.unlock()

        for child in snapshot where isAlive(child) {
            signal(child, SIGTERM)
        }

        let grace = timeout ?? shutdownGrace ?? 0
        let graceDeadline = ProcessInfo.processInfo.systemUptime + grace
        while snapshot.contains(where: isAlive)
            && ProcessInfo.processInfo.systemUptime < graceDeadline {
            Thread.sleep(forTimeInterval: 0.05)
        }

        for child in snapshot where isAlive(child) {
            log("force-killing child group pid=\(child.process.processIdentifier) after shutdown timeout")
            signal(child, SIGKILL)
        }

        let killDeadline = ProcessInfo.processInfo.systemUptime + 5
        while snapshot.contains(where: isAlive)
            && ProcessInfo.processInfo.systemUptime < killDeadline {
            Thread.sleep(forTimeInterval: 0.05)
        }
        let survivors = snapshot.filter(isAlive)
        if !survivors.isEmpty {
            log("child process group remained after SIGKILL: \(survivors.map { $0.process.processIdentifier })")
        }

        lock.lock()
        state = .finished
        lock.unlock()
        try? FileManager.default.removeItem(atPath: timeoutStatePath)
        return true
    }

    func waitForever() {
        RunLoop.current.run()
    }
}

let supervisor = Supervisor()

signal(SIGTERM, SIG_IGN)
signal(SIGINT, SIG_IGN)
let termSource = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
termSource.setEventHandler {
    if supervisor.shutdownAndWait() {
        exit(0)
    }
}
termSource.resume()
let intSource = DispatchSource.makeSignalSource(signal: SIGINT, queue: .main)
intSource.setEventHandler {
    if supervisor.shutdownAndWait() {
        exit(0)
    }
}
intSource.resume()

supervisor.start()
supervisor.waitForever()
