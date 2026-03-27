-- Atomic hot queries tracking with fair trimming
-- KEYS[1]: daily_key (sorted set)
-- KEYS[2]: params_key (string for params storage)
-- ARGV[1]: params_hash
-- ARGV[2]: max_entries
-- ARGV[3]: ttl_seconds
-- ARGV[4]: params_json

local daily_key = KEYS[1]
local params_key = KEYS[2]
local params_hash = ARGV[1]
local max_entries = tonumber(ARGV[2])
local ttl_seconds = tonumber(ARGV[3])
local params_json = ARGV[4]

-- Increment score
local new_score = redis.call('ZINCRBY', daily_key, 1, params_hash)

-- Get current size
local current_size = redis.call('ZCARD', daily_key)

-- Fair trimming: allow 20% buffer, then clean up garbage first
local capacity_threshold = max_entries * 1.2

if current_size > capacity_threshold then
    -- Remove one-time queries (score < 2)
    redis.call('ZREMRANGEBYSCORE', daily_key, '-inf', '(2')

    -- Check size again after garbage collection
    current_size = redis.call('ZCARD', daily_key)

    -- If still over max, trim to top N
    if current_size > max_entries then
        redis.call('ZREMRANGEBYRANK', daily_key, 0, -(max_entries + 1))
    end
end

-- Set TTLs and store params atomically
redis.call('EXPIRE', daily_key, ttl_seconds)
redis.call('SETEX', params_key, ttl_seconds, params_json)

return new_score
