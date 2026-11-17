--[[
  Rejoin Hub detector (runs in the executor's auto-execute folder).
  It only writes one small status file per account: <workspace>/RejoinHub/hb_<userId>.json, overwritten every 10 s
  and right away when Roblox shows an error. No network calls. The phone agent reads the file to decide whether the
  account is in game, kicked or disconnected.
  It also reads <workspace>/RejoinHub/cfg_<userId>.json, written by the agent, for per-account options:
  {"render3d": false, "fps": 15, "strip": true}. These are AFK clones, so by default (no file yet) 3D rendering is OFF (the game and
  scripts keep running, the screen stays dark: the biggest GPU/CPU saving) and the frame rate is capped at 15 with
  the executor's setfpscap. Roblox ignores FPS flags since its 2025 Fast Flag allowlist, so this is the cap that works.
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
local fps = nil
local DEFAULT_FPS = 15

local function setRender(want)
	if want ~= render3d then
		render3d = want
		pcall(function() RunService:Set3dRenderingEnabled(want) end)
	end
end

local function setFps(n)
	n = tonumber(n)
	if not n or n == fps then return end
	fps = n
	local cap = (type(setfpscap) == "function" and setfpscap) or (env and type(env.setfpscap) == "function" and env.setfpscap)
	if cap then
		pcall(cap, n > 0 and n or 0)   -- 0 = no cap
	end
end

-- AFK defaults right away, before the agent's file is read: dark screen, low frame rate, no sound, lowest quality.
setRender(false)
setFps(DEFAULT_FPS)
pcall(function()
	local gs = UserSettings():GetService("UserGameSettings")
	gs.MasterVolume = 0
	gs.SavedQualityLevel = Enum.SavedQualitySetting.QualityLevel1
end)
pcall(function() settings().Rendering.QualityLevel = Enum.QualityLevel.Level01 end)

-- World strip (default on, cfg "strip"): delete everything that only exists to be seen or heard and make every part
-- plain. Collision geometry stays, so the character keeps standing and farm scripts keep finding their parts.
local VISUAL = {
	Decal = true, Texture = true, SurfaceAppearance = true, ParticleEmitter = true, Trail = true, Beam = true,
	Smoke = true, Fire = true, Sparkles = true, Explosion = true, PointLight = true, SpotLight = true,
	SurfaceLight = true, Highlight = true, Sound = true, Shirt = true, Pants = true, ShirtGraphic = true,
	Sky = true, Atmosphere = true, Clouds = true, BloomEffect = true, BlurEffect = true, ColorCorrectionEffect = true,
	SunRaysEffect = true, DepthOfFieldEffect = true,
}
local stripping = false
local stripHooked = false
local function stripOne(o)
	local cls = o.ClassName
	if VISUAL[cls] then
		pcall(o.Destroy, o)
	elseif o:IsA("BasePart") then
		pcall(function()
			o.Material = Enum.Material.SmoothPlastic
			o.Reflectance = 0
			o.CastShadow = false
			if o:IsA("MeshPart") then o.TextureID = "" end
		end)
	elseif cls == "SpecialMesh" then
		pcall(function() o.TextureId = "" end)
	end
end
local function startStrip()
	if stripping then return end
	stripping = true
	local Lighting = game:GetService("Lighting")
	pcall(function()
		Lighting.GlobalShadows = false
		Lighting.Brightness = 0
		Lighting.EnvironmentDiffuseScale = 0
		Lighting.EnvironmentSpecularScale = 0
	end)
	pcall(function()
		local t = workspace.Terrain
		t.Decoration = false
		t.WaterWaveSize = 0
		t.WaterWaveSpeed = 0
		t.WaterReflectance = 0
		t.WaterTransparency = 1
	end)
	for _, root in ipairs({ workspace, Lighting, game:GetService("SoundService") }) do
		task.spawn(function()
			for i, o in ipairs(root:GetDescendants()) do
				if not alive() then return end
				pcall(stripOne, o)
				if i % 2000 == 0 then task.wait() end   -- big maps: spread the first pass over frames
			end
		end)
		if not stripHooked then
			table.insert(env.__RH_CONNS, root.DescendantAdded:Connect(function(o)
				if stripping then pcall(stripOne, o) end
			end))
		end
	end
	stripHooked = true
end

-- Options from the agent. Missing file or bad JSON means "keep the current state".
local function applyConfig()
	if type(readfile) ~= "function" or type(isfile) ~= "function" then return end
	local ok, raw = pcall(function()
		return isfile(CFG) and readfile(CFG) or nil
	end)
	if not ok or not raw or raw == "" then return end
	local ok2, cfg = pcall(function() return HttpService:JSONDecode(raw) end)
	if not ok2 or type(cfg) ~= "table" then return end
	if cfg.render3d ~= nil then setRender(cfg.render3d == true) end
	if cfg.fps ~= nil then setFps(cfg.fps) end
	if cfg.strip == false then stripping = false elseif cfg.strip == true then startStrip() end
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
		render3d = render3d == true,
		fps = fps,
		strip = stripping,
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

startStrip()     -- AFK default; cfg {"strip": false} stops it for new objects
applyConfig()
save()
task.spawn(function()
	while alive() do
		task.wait(INTERVAL)
		applyConfig()
		save()
	end
end)
