from aws_cdk import (
    App,
    CfnOutput,
    Duration,
    Stack,
)
from aws_cdk import (
    aws_autoscaling as autoscaling,
)
from aws_cdk import (
    aws_ec2 as ec2,
)
from aws_cdk import (
    aws_elasticloadbalancingv2 as elbv2,
)
from aws_cdk import (
    aws_iam as iam,
)
from constructs import Construct


class CatalogServerStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Create VPC with only public subnets (no NAT Gateway)
        vpc = ec2.Vpc(
            self,
            "CatalogVPC",
            max_azs=2,
            nat_gateways=0,
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="public", subnet_type=ec2.SubnetType.PUBLIC, cidr_mask=24
                )
            ],
        )

        # Create Security Group for the web/db server
        server_sg = ec2.SecurityGroup(
            self,
            "ServerSG",
            vpc=vpc,
            description="Allow traffic to catalog server",
            allow_all_outbound=True,
        )

        # Allow HTTP/HTTPS traffic from anywhere
        server_sg.add_ingress_rule(
            ec2.Peer.any_ipv4(), ec2.Port.tcp(80), "Allow HTTP traffic"
        )
        server_sg.add_ingress_rule(
            ec2.Peer.any_ipv4(), ec2.Port.tcp(443), "Allow HTTPS traffic"
        )

        # Allow MySQL traffic from within the security group
        server_sg.add_ingress_rule(
            server_sg,
            ec2.Port.tcp(3306),
            "Allow MySQL traffic from instances in the same group",
        )

        # Create IAM role for EC2 instances
        instance_role = iam.Role(
            self,
            "CatalogServerRole",
            assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "AmazonSSMManagedInstanceCore"
                )
            ],
        )

        # Create User Data for EC2 instances with MySQL installation
        user_data = ec2.UserData.for_linux()
        user_data.add_commands(
            "#!/bin/bash",
            "apt update && apt upgrade -y",
            # Install Nginx, Python, and MySQL
            "apt install nginx python3 python3-pip python3-venv mysql-server -y",
            # Configure MySQL
            "mysql_secure_installation << EOF\n\n\nn\ny\ny\ny\ny\nEOF",
            # Create MySQL database and user
            'mysql -e "CREATE DATABASE IF NOT EXISTS catalog;"',
            "mysql -e \"CREATE USER IF NOT EXISTS 'catalog_user'@'localhost' IDENTIFIED BY 'catalog_password';\"",
            "mysql -e \"GRANT ALL PRIVILEGES ON catalog.* TO 'catalog_user'@'localhost';\"",
            'mysql -e "FLUSH PRIVILEGES;"',
            # Create catalog table and insert sample data
            'mysql -e "USE catalog; CREATE TABLE IF NOT EXISTS products (id INT AUTO_INCREMENT PRIMARY KEY, name VARCHAR(255) NOT NULL, description TEXT, price DECIMAL(10,2) NOT NULL);"',
            "mysql -e \"USE catalog; INSERT INTO products (name, description, price) VALUES ('Laptop', 'A high-end laptop', 1200.00), ('Phone', 'Latest smartphone', 800.00);\"",
            # Set up Flask application
            "mkdir -p /home/ubuntu/catalog_server",
            "cd /home/ubuntu/catalog_server",
            "python3 -m venv venv",
            "source venv/bin/activate",
            "pip install flask flask_sqlalchemy mysqlclient gunicorn",
            # Create app.py
            "cat > /home/ubuntu/catalog_server/app.py << EOL",
            "from flask import Flask, jsonify",
            "from flask_sqlalchemy import SQLAlchemy",
            "",
            "app = Flask(__name__)",
            'app.config["SQLALCHEMY_DATABASE_URI"] = "mysql://catalog_user:catalog_password@localhost/catalog"',
            "db = SQLAlchemy(app)",
            "",
            "class Product(db.Model):",
            '    __tablename__ = "products"',
            "    id = db.Column(db.Integer, primary_key=True)",
            "    name = db.Column(db.String(255), nullable=False)",
            "    description = db.Column(db.Text)",
            "    price = db.Column(db.Float, nullable=False)",
            "",
            '@app.route("/products", methods=["GET"])',
            "def get_products():",
            "    products = Product.query.all()",
            '    return jsonify([{"id": p.id, "name": p.name, "description": p.description, "price": p.price} for p in products])',
            "",
            'if __name__ == "__main__":',
            '    app.run(host="0.0.0.0", port=5000)',
            "EOL",
            # Create Nginx config
            "cat > /etc/nginx/sites-available/catalog << EOL",
            "server {",
            "    listen 80;",
            "    server_name _;",
            "",
            "    location / {",
            "        proxy_pass http://127.0.0.1:5000;",
            "        proxy_set_header Host $host;",
            "        proxy_set_header X-Real-IP $remote_addr;",
            "        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;",
            "    }",
            "}",
            "EOL",
            "ln -s /etc/nginx/sites-available/catalog /etc/nginx/sites-enabled/",
            "rm -f /etc/nginx/sites-enabled/default",  # Remove default site to avoid conflicts
            "nginx -t && systemctl reload nginx",
            # Create systemd service
            "cat > /etc/systemd/system/catalog.service << EOL",
            "[Unit]",
            "Description=Catalog API Server",
            "After=network.target mysql.service",
            "",
            "[Service]",
            "User=ubuntu",
            "WorkingDirectory=/home/ubuntu/catalog_server",
            "ExecStart=/home/ubuntu/catalog_server/venv/bin/gunicorn --workers 3 --bind 0.0.0.0:5000 app:app",
            "Restart=always",
            "",
            "[Install]",
            "WantedBy=multi-user.target",
            "EOL",
            "systemctl daemon-reload",
            "systemctl start catalog",
            "systemctl enable catalog",
        )

        # Create launch template for EC2 instances
        launch_template = ec2.LaunchTemplate(
            self,
            "CatalogLaunchTemplate",
            machine_image=ec2.MachineImage.generic_linux(
                {
                    "eu-west-1": "ami-0261755bbcb8c4a84"  # Ubuntu 22.04 in us-east-1, update accordingly
                }
            ),
            instance_type=ec2.InstanceType.of(
                ec2.InstanceClass.T3, ec2.InstanceSize.MICRO
            ),  # T3.medium to handle both web and DB
            security_group=server_sg,
            user_data=user_data,
            role=instance_role,
        )

        # Create Auto Scaling Group
        asg = autoscaling.AutoScalingGroup(
            self,
            "CatalogASG",
            vpc=vpc,
            launch_template=launch_template,
            min_capacity=2,
            max_capacity=4,
            desired_capacity=2,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
            cooldown=Duration.minutes(3),
            health_check=autoscaling.HealthCheck.ec2(),
        )

        # Implement scaling policies
        asg.scale_on_cpu_utilization(
            "CpuScaling", target_utilization_percent=70, cooldown=Duration.minutes(3)
        )

        # Create Application Load Balancer
        alb = elbv2.ApplicationLoadBalancer(
            self,
            "CatalogALB",
            vpc=vpc,
            internet_facing=True,
            load_balancer_name="catalog-alb",
        )

        # Add a listener and target group
        listener = alb.add_listener("Listener", port=80, open=True)

        listener.add_targets(
            "WebTarget",
            port=80,
            targets=[asg],
            health_check=elbv2.HealthCheck(
                path="/products",
                interval=Duration.seconds(30),
                timeout=Duration.seconds(5),
            ),
        )

        # Output the ALB DNS name
        CfnOutput(self, "LoadBalancerDNS", value=alb.load_balancer_dns_name)


app = App()
CatalogServerStack(app, "CatalogServerStack")
app.synth()

