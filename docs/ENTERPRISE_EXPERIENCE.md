# Enterprise Experience Showcase

---

## 1. A Distributed System I've Built

I built the entire automated nudges microservice at my company. We had about 10-15 tenants using it, each with anywhere from 15 to 200 users. The idea was simple enough: look at how people are using the product, figure out who's disengaging, and send them the right nudge at the right time.

We broke users into three buckets. P4W (past 4 weeks) were people who'd been active recently. NP4W (non-past-4-weeks) were folks who used to be active but dropped off. And then there was Cold Start, which worked completely differently from the other two. P4W and NP4W ran on a schedule through Celery Beat, but Cold Start was reactive. If someone hadn't talked to our Teams bot in 24 hours, the next time they opened it, boom, they'd get a cold start nudge right there. If they were already chatting regularly, they'd never see one.

The whole thing ran on Django with Celery handling the async work. Every tenant had their own nudge limits and delivery windows, so the config side of it got pretty involved. Looking back, it's actually a lot like this document search service. Same multi-tenant model, same pattern of background workers pulling from a queue, same need to keep each tenant's stuff separate.

## 2. A Performance Optimization

We kept getting reports of users receiving the same cold start nudge two or three times in a row. Since cold start fires the moment someone comes back to the Teams bot, what was happening was the bot would sometimes send duplicate webhook callbacks, or the user would open multiple conversations quickly. Either way, multiple Celery workers would grab tasks for the same user at the same time, and each one would send a nudge. Not great when the whole point is a single well-timed message.

The fix was a lock table in the database. Before a worker processes a nudge for any user, it tries to insert that user's ID into the table. If the insert works, great, that worker gets to proceed. If the row already exists, some other worker is already on it, so this one backs off. Once the nudge is delivered, the worker deletes the row. Pretty straightforward. On top of that, I added daily nudge caps per tenant since each client had a different tolerance for how many nudges their users should get in a day. After rolling both changes out, the duplicate complaints stopped completely.

## 3. A Production Incident I Resolved

We had this "create allocation" flow. It's one of the core things the product does, and one day it just started being painfully slow. Users were sitting there waiting, some were timing out.

I dug into it and the issue was all in the database function behind the flow. It wasn't one big problem, it was a bunch of small ones stacked on top of each other. The query was doing `SELECT *` when we only needed four or five columns. There were joins to tables that didn't even contribute to the final result. Someone had used `ANY` inside a join condition, which was killing the query planner. There were subqueries that ran once per row when they could've been materialised CTEs. No indexes on the columns we were filtering by most.

I went through it piece by piece. Stripped the column list down, pulled out the dead joins, swapped the `ANY` for proper `IN` clauses, materialised the repeated subqueries, and added indexes where they mattered. I tested each change on its own to make sure it was actually helping and not just shuffling the problem around. By the time I was done, the flow was back to being fast enough that nobody complained about it anymore. Honestly the biggest takeaway was just: don't guess, profile. It's never one thing, it's always six small things compounding.

## 4. An Architectural Decision

Our tool had this analytics pipeline where users would send a config payload, the backend would turn it into a DB query, run it, crunch some numbers, and send the result back. For big tenants this could take a few seconds, so we added caching in Redis. Two levels: one at the config level (same config = cache hit) and another at the query level (different configs that produce the same underlying SQL also get a cache hit).

Worked well until we needed to refresh the cache. The data underneath changes, and different clients needed refreshes at different intervals. The original approach was basically "blow away everything for this tenant and regenerate it all at once." Problem was, while that refresh was running, the whole tool was blocked. Nobody could run queries until it finished. For bigger tenants this took minutes.

What I proposed instead was to keep track of all the Redis keys in a DB table. Tenant ID, config hash, when it was last refreshed. When it's time to refresh, instead of regenerating everything, just pull the keys from the DB table and delete them from Redis one by one. The cache repopulates lazily next time someone actually requests that data. Most of the cached entries that got cleared would never even get re-requested, so we were wasting compute regenerating stuff nobody needed. I also set up an LRU eviction policy on Redis as a safety net for anything the DB table missed. The whole refresh went from "minutes of downtime" to "a few seconds of background cleanup that users don't even notice."

---
