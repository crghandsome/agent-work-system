-- 创建应用账号（应用密码由环境变量在容器启动时注入不了 SQL，这里用占位符由 init 脚本模板处理不了，
-- 因此采用 MySQL 官方镜像约定：SQL 内直接创建，密码与 .env 中 MYSQL_APP_PASSWORD 保持一致）
CREATE USER IF NOT EXISTS 'aws_app'@'%' IDENTIFIED BY 'aws_app_7k4m8x';
GRANT ALL PRIVILEGES ON agent_work_system.* TO 'aws_app'@'%';
FLUSH PRIVILEGES;
