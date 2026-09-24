-- ============================================================
-- MoneyMoney Web Banking Extension
-- SCB (Siam Commercial Bank) Thailand: statement PDFs via local bridge
-- Version: 1.01
--
-- Changes in 1.01:
--  - Statements without transactions are accepted. SCB prints no balance on
--    them, so an account whose balance is not known yet says so instead of
--    showing a balance nobody has read from a statement.
-- ============================================================
--
-- SCB closed its internet banking in July 2023 and offers no API, so this
-- extension does not log in anywhere. It asks the local bridge (scb_bridge.py
-- on https://127.0.0.1:8766) to read the statement PDFs that the SCB EASY app
-- emails on request, and books what the bridge has reconciled.
--
-- Credentials in MoneyMoney: the user name is free text, the password is the
-- statement PDF password. It is handed to the bridge with every refresh and
-- never stored by the bridge.

WebBanking {
  version     = 1.01,
  url         = "https://127.0.0.1:8766",
  services    = {"SCB Thailand (Statement PDF)"},
  description = "SCB (Siam Commercial Bank) Thailand: imports statement PDFs through a local bridge"
}

local BRIDGE  = "https://127.0.0.1:8766"
local SERVICE = "SCB Thailand (Statement PDF)"
local SECONDS_PER_DAY = 86400

local connection
local statementPassword

-- ============================================================
-- Dates
--
-- Booking dates must never depend on the Mac's time zone: os.time() interprets
-- its fields locally, so the same booking day would yield a different timestamp
-- after a time zone change or a DST switch, and MoneyMoney would import the
-- transactions a second time. Anchoring at 12:00 UTC keeps the timestamp
-- constant and still resolves to the correct calendar day in every time zone
-- from UTC-11 to UTC+11.
-- ============================================================

local function utcOffsetAt(ts)
  local l = os.date("*t",  ts)
  local u = os.date("!*t", ts)
  local dayDiff = l.day - u.day
  if     dayDiff >  1 then dayDiff = -1   -- local is in the previous month
  elseif dayDiff < -1 then dayDiff =  1   -- UTC is in the previous month
  end
  return dayDiff * SECONDS_PER_DAY
       + (l.hour - u.hour) * 3600
       + (l.min  - u.min)  * 60
       + (l.sec  - u.sec)
end

local function dayTimestamp(isoDate)
  local y, m, d = tostring(isoDate):match("^(%d%d%d%d)%-(%d%d)%-(%d%d)$")
  if not y then error("Unexpected booking date from the bridge: " .. tostring(isoDate)) end
  local noonLocal = os.time({
    year = math.tointeger(tonumber(y)) --[[@as integer]],
    month = math.tointeger(tonumber(m)) --[[@as integer]],
    day = math.tointeger(tonumber(d)) --[[@as integer]],
    hour = 12, min = 0, sec = 0,
  })
  return noonLocal + utcOffsetAt(noonLocal)
end

-- ============================================================
-- Bridge
-- ============================================================

-- One request to the bridge. Returns the decoded JSON, or nil and a message
-- that is fit to be shown to the user.
local function bridge(method, path, headers)
  local ok, content = pcall(function()
    local body, bodyType = nil, nil
    if method == "POST" then body, bodyType = "", "application/json" end
    return connection:request(method, BRIDGE .. path, body, bodyType, headers)
  end)
  if not ok then
    return nil, "The SCB bridge did not answer on " .. BRIDGE .. ". " ..
      "Is it installed (install.sh) and is its certificate trusted? Details: " .. tostring(content)
  end
  local parsed, data = pcall(function() return JSON(content):dictionary() end)
  if not parsed or type(data) ~= "table" then
    return nil, "The SCB bridge sent no JSON for " .. path .. "."
  end
  if data.error then
    return nil, "SCB bridge: " .. tostring(data.error)
  end
  return data
end

-- ============================================================
-- MoneyMoney entry points
-- ============================================================

function SupportsBank(protocol, bankCode)
  return protocol == ProtocolWebBanking and bankCode == SERVICE
end

function InitializeSession(protocol, bankCode, username, reserved, password)
  connection = Connection()
  statementPassword = password

  local status, err = bridge("GET", "/__status__")
  if not status then return err end
  print("SCB bridge " .. tostring(status.version) .. ", inbox " .. tostring(status.inbox))
  if status.inboxError and status.inboxError ~= "" then
    return "The SCB bridge cannot read its inbox folder " .. tostring(status.inbox) .. " (" ..
      tostring(status.inboxError) .. "). Allow Python to access it under System Settings, Privacy, Files and Folders."
  end

  -- Let the bridge read every statement waiting in the inbox before accounts
  -- and balances are asked for.
  local result, refreshErr = bridge("POST", "/refresh", { ["X-Statement-Password"] = statementPassword })
  if not result then return refreshErr end

  for _, entry in ipairs(result.processed or {}) do
    print(string.format("Read %s: account %s, %s to %s, %s rows, %s new",
      entry.file, entry.account, entry.from, entry.to, tostring(entry.rows), tostring(entry.new)))
  end
  local failed = result.failed or {}
  if #failed > 0 then
    local lines = {}
    for _, entry in ipairs(failed) do
      if tostring(entry.error):find("password does not open") then
        return LoginFailed
      end
      table.insert(lines, entry.file .. ": " .. tostring(entry.error))
    end
    return "A statement in the inbox could not be read. Move it away or fix it:\n" .. table.concat(lines, "\n")
  end
  return nil
end

function ListAccounts(knownAccounts)
  local data, err = bridge("GET", "/accounts")
  if not data then return err end
  local accounts = {}
  for _, a in ipairs(data.accounts or {}) do
    table.insert(accounts, {
      name          = "SCB " .. a.accountNumber,
      owner         = a.owner,
      accountNumber = a.accountNumber,
      bankCode      = "SCB",
      currency      = a.currency or "THB",
      type          = (a.type == "current") and AccountTypeGiro or AccountTypeSavings,
    })
  end
  if #accounts == 0 then
    return "No statement has been read yet. Put a statement PDF from the SCB EASY app into the bridge's inbox folder and refresh again."
  end
  return accounts
end

function RefreshAccount(account, since)
  -- The whole history the bridge holds is delivered every time: MoneyMoney
  -- discards what it already has, and the store is a few hundred rows at most.
  -- Honouring `since` would leave older statements out after the first refresh.
  local data, err = bridge("GET", "/transactions?account=" .. MM.urlencode(account.accountNumber) .. "&since=1970-01-01")
  if not data then return err end

  -- The bridge knows a balance only from a statement that lists transactions.
  local balance = tonumber(data.balance)
  if balance == nil then
    return "The balance of SCB account " .. account.accountNumber .. " is not known yet: " ..
      "SCB prints no balance on a statement without transactions, and no statement read so far lists one. " ..
      "Request a statement over a longer period in the SCB EASY app (up to 12 months) " ..
      "that contains at least one transaction, put it into the inbox and refresh again."
  end

  local transactions = {}
  local order = {}  -- booking time per entry, only used to sort same-day rows
  for _, t in ipairs(data.transactions or {}) do
    local day = dayTimestamp(t.bookingDate)
    local entry = {
      name        = t.name,
      amount      = tonumber(t.amount),
      currency    = "THB",
      bookingDate = day,
      valueDate   = day,
      purpose     = t.purpose,
      bookingText = t.bookingText,
      booked      = true,
    }
    order[entry] = tostring(t.bookingDate) .. " " .. tostring(t.time)
    table.insert(transactions, entry)
  end
  -- Newest first, as MoneyMoney expects.
  table.sort(transactions, function(a, b) return order[a] > order[b] end)

  print(string.format("Account %s: balance %s THB as of %s, %d transactions",
    account.accountNumber, tostring(data.balance), tostring(data.balanceDate), #transactions))
  return {
    balance      = balance,
    transactions = transactions,
  }
end

function EndSession()
  statementPassword = nil
  connection = nil
end
