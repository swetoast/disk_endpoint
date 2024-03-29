## Disk Endpoint

Disk Endpoint is a Flask application that provides an endpoint to get disk information. It uses the nvme list and smartctl commands to gather information about NVMe drives and other drives in the system. The information includes the name, model, serial number, and health status of each disk.
Installation

Clone the repository:

     git clone https://github.com/yourusername/disk-endpoint.git
    
Navigate to the project directory:

      cd disk-endpoint
Install the required Python packages:
     
      pip install -r requirements.txt 

## Configuration

The application reads its configuration from a file named disk_endpoint.conf in the same directory. Here’s an example of what the configuration file might look like:
```
[DEFAULT]
HOST = 127.0.0.1
PORT = 5000
USE_HTTPS = False
CERTIFICATE_PATH = /path/to/certificate.pem
KEY_PATH = /path/to/key.pem
TOKEN = your_predefined_token
```
Replace /path/to/certificate.pem and /path/to/key.pem with the actual paths to your SSL certificate and key files if you are using HTTPS. Also, replace your_predefined_token with the actual token you want to use for authentication.
Running the Application

You can run the application with the following command:

sudo python app.py

## Using with Home Assistant

You can use Disk Endpoint with Home Assistant by setting up a RESTful sensor. Here’s an example of how you might set it up:

```yaml
sensor:
  - platform: rest
    resource: http://your_flask_app_url/disk_info?token=your_predefined_token
    name: Disk Info
    value_template: '{{ value_json.name }}'
    json_attributes:
      - model
      - serial
      - health_status
```
In this example, replace http://your_flask_app_url/disk_info?token=your_predefined_token with the actual URL of your Flask app’s endpoint and your actual token. The value_template is used to extract the value that will be displayed for the sensor in Home Assistant, and json_attributes is used to extract additional attributes from the JSON response.
## Systemd Service

You can also run the application as a systemd service. Here’s an example of a systemd service file:
```systemd
[Unit]
Description=Disk Endpoint Flask Application
After=network.target

[Service]
User=root
WorkingDirectory=/path/to/your/flask/app
ExecStart=/path/to/your/python/environment/bin/python app.py
Restart=always

[Install]
WantedBy=multi-user.target
```
In this example, replace the following placeholders with your actual values:

yourusername: The username of the user that will run the service.

/path/to/your/flask/app: The path to the directory containing your Flask application.

/path/to/your/python/environment/bin/python: The path to the Python executable in your virtual environment.

To use this service file:

Save it as disk-endpoint.service in the /etc/systemd/system directory.

Enable the service to start on boot with the command sudo systemctl enable disk-endpoint.

Start the service with the command sudo systemctl start disk-endpoint.

You can check the status of the service with the command sudo systemctl status disk-endpoint.
## License

This project is licensed under the terms of The Unlicense.
