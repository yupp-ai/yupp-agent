local key = KEYS[1]
local limit = tonumber(ARGV[1])
local interval = tonumber(ARGV[2])
local burst_ratio = tonumber(ARGV[3])

local now = redis.call('TIME')[1]
local tokens = redis.call('hget', key, 'tokens')
local last_update = redis.call('hget', key, 'last_update')

if tokens == false then
    tokens = limit
    last_update = now
else
    tokens = tonumber(tokens)
    last_update = tonumber(last_update)
    local delta = now - last_update
    tokens = math.min(limit * burst_ratio, tokens + delta * limit / interval)
end

if tokens >= 1 then
    tokens = tokens - 1
    redis.call('hset', key, 'tokens', tokens)
    redis.call('hset', key, 'last_update', now)
    redis.call('expire', key, math.min(interval * burst_ratio*2, 3600))  -- Auto-cleanup
    return 1
else
    return 0
end
