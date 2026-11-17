--[[
  Rejoin Hub detector (runs in the executor's auto-execute folder).
  It only writes one small status file per account: <workspace>/RejoinHub/hb_<userId>.json, overwritten every 10 s
  and right away when Roblox shows an error. No network calls. The phone agent reads the file to decide whether the
  account is in game, kicked or disconnected.
  It also reads <workspace>/RejoinHub/cfg_<userId>.json, written by the agent, for per-account options:
  {"render3d": false} turns 3D rendering off (the game keeps running; the screen stays blank, saving GPU and RAM).
]]

local VERSION = 1
local INTERVAL = 10

local env = (getgenv and getgenv()) or _G
-- A new run supersedes older ones: their loops see a different generation number and stop.
env.__RH_GEN = (env.__RH_GEN or 0) + 1
local GEN = env.__RH_GEN
if env.__RH_CONNS then
	for _, c in ipairs(env.__RH_CONNS) do
		pcall(function() c:Disconnect() end)
	end
end
env.__RH_CONNS = {}

local function alive()
	return env.__RH_GEN == GEN
end

if not game:IsLoaded() then
	game.Loaded:Wait()
end

local Players = game:GetService("Players")
local GuiService = game:GetService("GuiService")
local CoreGui = game:GetService("CoreGui")
local HttpService = game:GetService("HttpService")
local TeleportService = game:GetService("TeleportService")
local RunService = game:GetService("RunService")

local LP = Players.LocalPlayer
while not LP and alive() do
	task.wait(0.5)
	LP = Players.LocalPlayer
end
if not alive() then return end

local FOLDER = "RejoinHub"
local FILE = FOLDER .. "/hb_" .. tostring(LP.UserId) .. ".json"
local CFG = FOLDER .. "/cfg_" .. tostring(LP.UserId) .. ".json"
local BOOT = os.time()
local lastError = nil
local render3d = nil

-- Options from the agent. Missing file or bad JSON means "keep the current state".
local function applyConfig()
	if type(readfile) ~= "function" or type(isfile) ~= "function" then return end
	local ok, raw = pcall(function()
		return isfile(CFG) and readfile(CFG) or nil
	end)
	if not ok or not raw or raw == "" then return end
	local ok2, cfg = pcall(function() return HttpService:JSONDecode(raw) end)
	if not ok2 or type(cfg) ~= "table" then return end
	local want = cfg.render3d ~= false
	if want ~= render3d then
		render3d = want
		pcall(function() RunService:Set3dRenderingEnabled(want) end)
	end
end

local function canWrite()
	return type(writefile) == "function"
end

local function save()
	if not canWrite() or not alive() then return end
	local data = {
		v = VERSION,
		ts = os.time(),
		boot = BOOT,
		userId = tostring(LP.UserId),
		user = LP.Name,
		placeId = tostring(game.PlaceId),
		jobId = tostring(game.JobId),
		render3d = render3d ~= false,
		err = lastError,
	}
	pcall(function()
		if type(isfolder) == "function" and type(makefolder) == "function" and not isfolder(FOLDER) then
			makefolder(FOLDER)
		end
		writefile(FILE, HttpService:JSONEncode(data))
	end)
end

-- Error classification. Order matters: the first match wins, and "disconnect" is the catch-all.
local KINDS = {
	{ "key_expired", { "key is invalid", "key has expired", "key expired", "kunci sudah kadaluarsa", "kunci tidak valid" } },
	{ "same_account", { "same account", "akun yang sama", "perangkat yang berbeda", "logged in from another", "another device" } },
	{ "banned", { "banned", "dilarang", "account has been" } },
	{ "kicked", { "you were kicked", "kicked from", "kicked by", "telah dikeluarkan" } },
	{ "shutdown", { "server has shut down", "server telah dimatikan", "error code: 288", "kode error: 288" } },
	{ "idle", { "you have been idle", "telah idle", "error code: 290", "kode error: 290" } },
	{ "full", { "server is full", "game is full", "experience is full", "penuh" } },
	{ "updating", { "roblox is updating", "please update", "outdated" } },
	{ "disconnect", { "disconnected", "lost connection", "connection lost", "koneksi terputus", "hubungkan kembali", "lost the connection", "reconnect" } },
}

local function promptText()
	local parts = {}
	pcall(function()
		local gui = CoreGui:FindFirstChild("RobloxPromptGui")
		if not gui then return end
		local root = gui:FindFirstChild("promptOverlay") or gui
		for _, d in ipairs(root:GetDescendants()) do
			if (d:IsA("TextLabel") or d:IsA("TextButton") or d:IsA("TextBox")) and d.Text ~= "" then
				table.insert(parts, d.Text)
			end
		end
	end)
	local text = table.concat(parts, " | ")
	if text == "" then
		pcall(function() text = GuiService:GetErrorMessage() or "" end)
	end
	return text
end

local function classify(text)
	local low = string.lower(text):gsub("%s+", " ")
	for _, k in ipairs(KINDS) do
		for _, pat in ipairs(k[2]) do
			if string.find(low, pat, 1, true) then
				return k[1]
			end
		end
	end
	return "disconnect"
end

local function code(text)
	local c = string.match(text, "[Ee]rror [Cc]ode:?%s*(%d+)") or string.match(text, "[Kk]ode [Ee]rror:?%s*(%d+)")
		or string.match(text, "%((%d%d%d)%)")
	return c and tonumber(c) or 0
end

local function report(kind, c, text)
	lastError = { kind = kind, code = c, text = string.sub(text or "", 1, 240), ts = os.time() }
	save()
end

table.insert(env.__RH_CONNS, GuiService.ErrorMessageChanged:Connect(function()
	task.wait(0.3)
	if not alive() then return end
	local text = promptText()
	if text == "" then return end
	report(classify(text), code(text), text)
end))

table.insert(env.__RH_CONNS, TeleportService.TeleportInitFailed:Connect(function(player, result, msg)
	if player ~= LP or not alive() then return end
	local r = tostring(result)
	report("teleport_failed", 769, r .. " " .. tostring(msg or ""))
end))

-- Anti-idle: Roblox kicks after 20 idle minutes; a virtual click resets the timer. Set RH_ANTI_AFK = false to skip.
if env.RH_ANTI_AFK ~= false then
	pcall(function()
		local VirtualUser = game:GetService("VirtualUser")
		table.insert(env.__RH_CONNS, LP.Idled:Connect(function()
			if not alive() then return end
			VirtualUser:CaptureController()
			VirtualUser:ClickButton2(Vector2.new())
		end))
	end)
end

applyConfig()
save()
task.spawn(function()
	while alive() do
		task.wait(INTERVAL)
		applyConfig()
		save()
	end
end)
