-- ============================================
-- Meeting Follow-Up Tasks Table
-- Target: Microsoft Fabric SQL Database
-- ============================================

CREATE TABLE dbo.MeetingFollowUpTasks
(
    -- Primary Key
    task_id                 NVARCHAR(20)        NOT NULL,   -- e.g., TASK-20260316-001

    -- Core Fields (Helper Agent opening message)
    follow_up_task          NVARCHAR(MAX)       NOT NULL,   -- Task description
    task_owner              NVARCHAR(200)       NULL,       -- NULL when ownership is ambiguous\\
    task_owner_email        varchar(200) null,
    context_summary         NVARCHAR(MAX)       NULL,       -- 2-3 sentence background from meeting
    suggested_ai_actions    NVARCHAR(MAX)       NULL,       -- JSON array: ["action1", "action2"]

    -- Supplementary Fields (Helper Agent on-demand)
    related_people          NVARCHAR(MAX)       NULL,       -- JSON array: [{"name":"","role":"","relevance":""}]
    transcript_segments     NVARCHAR(MAX)       NULL,       -- JSON array: [{"start":"","end":"","topic":""}]

    -- Management Fields
    task_status             NVARCHAR(20)        NOT NULL    DEFAULT 'Pending',  -- Pending | Completed | Ignored
    meeting_name            NVARCHAR(500)       NULL,
    meeting_date            DATE                NULL,

    -- Audit Fields
    created_at              DATETIME2           NOT NULL    DEFAULT SYSUTCDATETIME(),
    updated_at              DATETIME2           NOT NULL    DEFAULT SYSUTCDATETIME(),

    -- Constraints
    CONSTRAINT PK_MeetingFollowUpTasks PRIMARY KEY (task_id),
    CONSTRAINT CK_TaskStatus CHECK (task_status IN ('Pending', 'Completed', 'Ignored'))
);

-- ============================================
-- Index for common query patterns
-- ============================================

-- Helper Agent queries by task_id + task_owner (RLS filtered)
CREATE INDEX IX_TaskOwner ON dbo.MeetingFollowUpTasks (task_owner, task_status);

-- Logic Apps queries for pending tasks to send Adaptive Cards
CREATE INDEX IX_PendingTasks ON dbo.MeetingFollowUpTasks (task_status, created_at)
    WHERE task_status = 'Pending';

-- Query by meeting
CREATE INDEX IX_MeetingDate ON dbo.MeetingFollowUpTasks (meeting_date, meeting_name);

-- ============================================
-- Row-Level Security (RLS) for OBO access
-- ============================================

-- Security predicate function: users can only see their own tasks

CREATE OR ALTER FUNCTION dbo.fn_TaskOwnerFilter(@task_owner_email NVARCHAR(200))
RETURNS TABLE
WITH SCHEMABINDING
AS
    RETURN SELECT 1 AS result
    WHERE 
        -- 規則 1: 任務擁有者是本人
        @task_owner_email = USER_NAME() 
        
        -- 規則 2: 或是任務尚未指派 (NULL)
        OR @task_owner_email IS NULL 
        
        -- 規則 3: 特例 - 該特定使用者不受限，可查看所有資料
        OR USER_NAME() = '626860b4-b57e-4e59-aaad-4556f0bd6333@b9bbf971-ad4d-4759-acf5-93dc3599c3f6';

-- Apply security policy
CREATE SECURITY POLICY dbo.TaskOwnerPolicy
    ADD FILTER PREDICATE dbo.fn_TaskOwnerFilter(task_owner_email)
    ON dbo.MeetingFollowUpTasks
    WITH (STATE = ON);

-- ============================================
-- Auto-update updated_at on row modification
-- ============================================

-- CREATE TRIGGER trg_UpdateTimestamp
-- ON dbo.MeetingFollowUpTasks
-- AFTER UPDATE
-- AS
-- BEGIN
--     SET NOCOUNT ON;
--     UPDATE t
--     SET updated_at = SYSUTCDATETIME()
--     FROM dbo.MeetingFollowUpTasks t
--     INNER JOIN inserted i ON t.task_id = i.task_id;
-- END;


-- 停用
ALTER SECURITY POLICY dbo.TaskOwnerPolicy  
WITH (STATE = OFF);

-- 執行你的操作...

-- 重新啟用
ALTER SECURITY POLICY dbo.TaskOwnerPolicy  
WITH (STATE = ON);


ALTER SECURITY POLICY dbo.TaskOwnerPolicy
DROP FILTER PREDICATE ON dbo.MeetingFollowUpTasks;